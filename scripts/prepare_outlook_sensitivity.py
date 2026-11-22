"""Prepare offline renewable weather sensitivity ranges from the bundled replay.

This does not modify the replay pack, fetch data, or estimate prediction intervals.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gridmind.datasets import wind_yield

SOURCE = ROOT / "data" / "austin_public.json"
DESTINATION = ROOT / ".runtime" / "analysis" / "austin_renewable_sensitivity.json"
DEFAULT_PV_DELTA = 0.20
DEFAULT_WIND_DELTA_MS = 1.0
CUT_IN_MS = 3.0
RATED_MS = 12.0
CUT_OUT_MS = 25.0
FACTORS = ("solar", "wind")


def finite_in(value: float, low: float, high: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def factor_value(value: float | None, name: str) -> float | None:
    return None if value is None else finite_in(value, 0.0, 1.0, name)


def solar_range(point: float | None, delta: float) -> tuple[float, float] | None:
    if point is None:
        return None
    return max(0.0, point * (1 - delta)), min(1.0, point * (1 + delta))


def inferred_hub_speed(point: float | None) -> float | None:
    # A zero output may come from calm wind or cut-out. Rated output is a plateau.
    if point is None or not 0 < point < 1:
        return None
    return (CUT_IN_MS ** 3 + point * (RATED_MS ** 3 - CUT_IN_MS ** 3)) ** (1 / 3)


def wind_range(point: float | None, speed: float | None, delta_ms: float) -> tuple[float, float] | None:
    if speed is None or point is None:
        return None
    lower, upper = max(0.0, speed - delta_ms), speed + delta_ms
    # Check endpoints plus both transitions, not just endpoints, because the
    # curve plateaus at rated power and drops to zero at the cut-out speed.
    critical = [v for v in (RATED_MS, CUT_OUT_MS) if lower <= v <= upper]
    outputs = [point, *(wind_yield(v) for v in (lower, upper, *critical))]
    return min(outputs), max(outputs)


def utc_time(value: str, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO UTC timestamp")
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an ISO UTC timestamp")
    return stamp


def stable_range(bounds: tuple[float, float] | None, point: float | None) -> list[float] | None:
    if bounds is None or point is None:
        return None
    # Keep the exact source point within the serialized envelope.
    return [min(round(bounds[0], 10), point), max(round(bounds[1], 10), point)]


def compare_vintages(ticks: list, outlooks: list) -> dict:
    current = {}
    for row in ticks:
        target = utc_time(row["forecast_valid_at"], "current forecast target")
        issued = utc_time(row["forecast_issued_at"], "current forecast issue")
        points = {name: factor_value(row.get(f"{name}_factor"), name) for name in FACTORS}
        known = current.setdefault(target, (issued, points))
        if known != (issued, points):
            raise ValueError(f"Conflicting target-time forecast for {target.isoformat()}")
    check = {"same_cycle": 0, "no_match": 0, "different_cycle": 0, "comparisons": []}
    for row in outlooks:
        target = utc_time(row["forecast_valid_at"], "outlook target")
        issued = utc_time(row["forecast_issued_at"], "outlook issue")
        if target not in current:
            check["no_match"] += 1
            continue
        current_issue, points = current[target]
        if current_issue == issued:
            check["same_cycle"] += 1
            if any(row[f"forecast_{name}_factor"] != points[name] for name in FACTORS):
                raise ValueError(f"Same forecast vintage has conflicting factors at {target.isoformat()}")
            continue
        check["different_cycle"] += 1
        comparisons = {
            "forecast_valid_at": target.isoformat(),
            "outlook_issue": issued.isoformat(),
            "target_time_issue": current_issue.isoformat(),
        }
        for name in FACTORS:
            older = row[f"forecast_{name}_factor"]
            target_time = points[name]
            band = row[f"{name}_scenario_factor_range"]
            comparisons[name] = {
                "outlook_factor": older,
                "target_time_factor": target_time,
                "revision": round(target_time - older, 10) if None not in (older, target_time) else None,
                "revision_value_within_scenario": band[0] <= target_time <= band[1]
                if band is not None and target_time is not None else None,
            }
        check["comparisons"].append(comparisons)
    return check


def prepare(source: Path, *, pv_delta: float = DEFAULT_PV_DELTA,
            wind_delta_ms: float = DEFAULT_WIND_DELTA_MS) -> dict:
    """Generate a deterministic report from archived point forecasts, not variation over time."""
    pv_delta = finite_in(pv_delta, 0, 1, "PV fraction delta")
    wind_delta_ms = finite_in(wind_delta_ms, 0, CUT_OUT_MS, "hub wind speed delta")
    raw = source.read_bytes()
    pack = json.loads(raw)
    manifest, ticks = pack["manifest"], pack["ticks"]
    if not ticks:
        raise ValueError("Replay pack contains no intervals")
    publication_delay = finite_in(manifest["weather_publication_delay_minutes"], 0, 1440,
                                  "weather publication delay in minutes")
    delay = timedelta(minutes=publication_delay)
    outlooks = {}
    for slot in ticks:
        instant = utc_time(slot["timestamp"], "replay slot")
        target = utc_time(slot["next_forecast_valid_at"], "outlook target")
        issued = utc_time(slot["next_forecast_issued_at"], "outlook issue")
        if not issued + delay <= instant < target:
            raise ValueError(f"Forecast is not available as of replay slot {instant.isoformat()}")
        points = {name: factor_value(slot.get(f"forecast_{name}_factor"), name) for name in FACTORS}
        solar, wind = points["solar"], points["wind"]
        stamp = (target, issued)
        if stamp in outlooks:
            known = outlooks[stamp]
            if any(known[f"forecast_{name}_factor"] != points[name] for name in FACTORS):
                raise ValueError(f"Conflicting outlook factors for {target.isoformat()} at {issued.isoformat()}")
            if instant.isoformat() < known["first_replay_slot_at"]:
                known["first_replay_slot_at"] = instant.isoformat()
            known["slot_count"] += 1
            continue
        speed = inferred_hub_speed(wind)
        outlooks[stamp] = {
            "first_replay_slot_at": instant.isoformat(),
            "forecast_valid_at": target.isoformat(),
            "forecast_issued_at": issued.isoformat(),
            "slot_count": 1,
            "forecast_solar_factor": solar,
            "forecast_wind_factor": wind,
            "inferred_hub_wind_ms": round(speed, 10) if speed is not None else None,
            "solar_scenario_factor_range": stable_range(solar_range(solar, pv_delta), solar),
            "wind_scenario_factor_range": stable_range(wind_range(wind, speed, wind_delta_ms), wind),
        }
    rows = [outlooks[key] for key in sorted(outlooks)]
    return {
        "schema_version": 1,
        "method": {
            "id": "archived-renewable-physical-sensitivity",
            "label": "Scenario range",
            "interpretation": "Modeled what-if bounds under fixed perturbations; no coverage claim.",
            "pv_relative_yield_delta": pv_delta,
            "wind_hub_speed_delta_ms": wind_delta_ms,
            "wind_cut_in_rated_cut_out_ms": [CUT_IN_MS, RATED_MS, CUT_OUT_MS],
        },
        "source": {
            "filename": source.name,
            "sha256": sha256(raw).hexdigest(),
            "forecast_archive": manifest["weather_archive"],
            "forecast_source": manifest["weather_source"],
            "gridpoint": manifest["weather_gridpoint"],
            "publication_delay_minutes": publication_delay,
        },
        "summary": {
            "replay_slots": len(ticks),
            "unique_target_forecasts": len(rows),
            "solar_scenario_count": sum(row["solar_scenario_factor_range"] is not None for row in rows),
            "wind_scenario_count": sum(row["wind_scenario_factor_range"] is not None for row in rows),
            "same_target_vintage_check": compare_vintages(ticks, rows),
        },
        "outlooks": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE, help="existing public replay JSON")
    parser.add_argument("--output", type=Path, default=DESTINATION, help="standalone sensitivity report")
    parser.add_argument("--pv-yield-delta", type=float, default=DEFAULT_PV_DELTA,
                        help="relative change to solar yield (default: ±0.20)")
    parser.add_argument("--wind-speed-delta", type=float, default=DEFAULT_WIND_DELTA_MS,
                        help="additive hub wind speed change in m/s (default: ±1.0)")
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error("the output must not overwrite the source replay pack")
    try:
        result = prepare(args.source, pv_delta=args.pv_yield_delta, wind_delta_ms=args.wind_speed_delta)
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output = json.dumps(result, indent=2, allow_nan=False) + "\n"
    args.output.write_text(output, encoding="utf-8")
    print(f"Prepared {result['summary']['unique_target_forecasts']} target forecasts at {args.output}")


if __name__ == "__main__":
    main()
