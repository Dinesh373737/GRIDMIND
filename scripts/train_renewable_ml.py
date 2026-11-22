"""Fit offline Austin forecast-to-HRRR-analysis error margins from checked-in inputs.

The analysis is a weather-model proxy, not actual renewable generation.
Training requires only Python 3.11+ and does not change the original replay.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import mean
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gridmind.datasets import solar_yield, wind_yield
from scripts.prepare_outlook_sensitivity import prepare as prepare_sensitivity

PACK = ROOT / "data" / "austin_public.json"
TRAINING = ROOT / "data" / "austin_renewable_ml_training.json"
ARTIFACT = ROOT / "data" / "austin_renewable_ml.json"
RIDGE_PENALTY = 0.10
CALIBRATION_TARGET = 0.80
LEAD_SCALE_HOURS = 13.0
SPLITS = {
    "fit": (date(2018, 8, 9), date(2018, 8, 15)),
    "calibration": (date(2018, 8, 16), date(2018, 8, 19)),
    "test": (date(2018, 8, 20), date(2018, 8, 23)),
}
MIN_DAYS = {"fit": 5, "calibration": 3, "test": 3}
LEADS = (2, 8, 13)
CYCLE = 12


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() != timedelta(0):
        raise ValueError("Forecast and analysis timestamps must be UTC")
    return result


def clip(value):
    return min(1.0, max(0.0, value))


def available(*values):
    return all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def power_factor(kind, weather):
    if kind == "solar":
        irradiance, kelvin = (weather.get(k) for k in ("irradiance_wm2", "temperature_kelvin"))
        return solar_yield(max(0.0, irradiance), kelvin - 273.15) if available(irradiance, kelvin) else None
    if kind == "wind":
        u, v = (weather.get(k) for k in ("hub_u_ms", "hub_v_ms"))
        return wind_yield(math.hypot(u, v)) if available(u, v) else None
    raise ValueError(f"Unknown renewable source: {kind}")


def features(point, lead_hours):
    return (1.0, point, point ** 2, lead_hours / LEAD_SCALE_HOURS)


def solve_system(matrix, rhs):
    """Partial-pivot Gaussian elimination for the four-column ridge system."""
    rows = [list(row) + [target] for row, target in zip(matrix, rhs)]
    size = len(rows)
    for col in range(size):
        pivot = max(range(col, size), key=lambda i: abs(rows[i][col]))
        if abs(rows[pivot][col]) < 1e-12:
            raise ValueError("The ridge system is singular")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        divisor = rows[col][col]
        rows[col] = [value / divisor for value in rows[col]]
        for row in range(size):
            if row != col:
                multiplier = rows[row][col]
                rows[row] = [a - multiplier * b for a, b in zip(rows[row], rows[col])]
    return [row[-1] for row in rows]


def fit_ridge(samples):
    vectors = [features(p, lead) for _, lead, p, _ in samples]
    errors = [abs(a - p) for _, _, p, a in samples]
    matrix = [
        [math.fsum(v[i] * v[j] for v in vectors) + (RIDGE_PENALTY if i == j and i > 0 else 0.0)
         for j in range(4)]
        for i in range(4)
    ]
    rhs = [math.fsum(v[i] * e for v, e in zip(vectors, errors)) for i in range(4)]
    return solve_system(matrix, rhs)


def predicted_error(coefficients, point, lead_hours):
    return clip(math.fsum(a * b for a, b in zip(coefficients, features(point, lead_hours))))


def calibration_addon(coefficients, samples):
    ordered = sorted(abs(a - p) - predicted_error(coefficients, p, lead)
                     for _, lead, p, a in samples)
    rank = math.ceil((len(ordered) + 1) * CALIBRATION_TARGET)
    if rank > len(ordered):
        raise ValueError("Too few calibration rows for the prescribed 80% residual rank")
    return max(0.0, ordered[rank - 1]), rank


def margin(coefficients, addon, point, lead_hours):
    return clip(predicted_error(coefficients, point, lead_hours) + addon)


def metrics(coefficients, addon, samples):
    errors, residuals, half_widths, widths, hits = [], [], [], [], []
    for _, lead, point, analysis in samples:
        half_width = margin(coefficients, addon, point, lead)
        lower, upper = clip(point - half_width), clip(point + half_width)
        error = abs(analysis - point)
        errors.append(error)
        residuals.append(abs(error - predicted_error(coefficients, point, lead)))
        half_widths.append(half_width)
        widths.append(upper - lower)
        hits.append(lower <= analysis <= upper)
    return {
        "n": len(samples),
        "issue_days": len({day for day, *_ in samples}),
        "point_mae_factor": round(mean(errors), 10),
        "uncalibrated_error_model_mae_factor": round(mean(residuals), 10),
        "analysis_hits": sum(hits),
        "analysis_coverage": round(mean(hits), 10),
        "mean_margin_factor": round(mean(half_widths), 10),
        "mean_clipped_width_factor": round(mean(widths), 10),
    }


def aligned_samples(training, earliest_replay_issue):
    result = {kind: {name: [] for name in SPLITS} for kind in ("solar", "wind")}
    seen = set()
    latest_targets = {name: [] for name in SPLITS}
    first_issues = {name: [] for name in SPLITS}
    for row in sorted(training["rows"], key=lambda row: (row["forecast_issued_at"], row["forecast_valid_at"])):
        issued, target = (stamp(row[k]) for k in ("forecast_issued_at", "forecast_valid_at"))
        day = issued.date()
        lead = (target - issued).total_seconds() / 3600.0
        if issued.hour != CYCLE or issued.minute or issued.second or issued.microsecond or lead not in LEADS:
            raise ValueError(f"Unexpected training cycle/lead at {issued.isoformat()} to {target.isoformat()}")
        if target >= earliest_replay_issue:
            raise ValueError("Training analysis must predate even the earliest bundled replay forecast issue")
        if (issued, target) in seen:
            raise ValueError("Duplicate same-target training pair")
        seen.add((issued, target))
        partition = next((name for name, (first, last) in SPLITS.items() if first <= day <= last), None)
        if partition is None:
            raise ValueError(f"Training forecast issue falls outside the fixed split: {day}")
        latest_targets[partition].append(target)
        first_issues[partition].append(issued)
        for kind, splits in result.items():
            forecast = power_factor(kind, row["forecast"])
            analysis = power_factor(kind, row["analysis"])
            if forecast is not None and analysis is not None:
                splits[partition].append((day, lead, forecast, analysis))
    partitions = list(SPLITS)
    for first, second in zip(partitions, partitions[1:]):
        if not latest_targets[first] or not first_issues[second] or max(latest_targets[first]) >= min(first_issues[second]):
            raise ValueError("Analysis targets of an earlier split must predate all forecasts of the next split")
    return result


def generate(pack, training_path):
    sensitivity = prepare_sensitivity(pack)
    pack_hash = sensitivity["source"]["sha256"]
    training_bytes = training_path.read_bytes().replace(b"\r\n", b"\n")
    training = json.loads(training_bytes)
    if (training.get("schema_version") != 1 or training.get("source_pack_sha256") != pack_hash
            or training.get("gridpoint") != sensitivity["source"]["gridpoint"]):
        raise ValueError("Training provenance does not match the replay pack, schema version, and gridpoint")
    earliest = min(stamp(row["forecast_issued_at"]) for row in sensitivity["outlooks"])
    samples = aligned_samples(training, earliest)
    models, trained = {}, {}
    for kind, sets in samples.items():
        days = {name: len({row[0] for row in data}) for name, data in sets.items()}
        if any(days[name] < MIN_DAYS[name] for name in SPLITS):
            raise ValueError(f"Too few distinct {kind} issue days: {days}")
        coefficients = fit_ridge(sets["fit"])
        addon, rank = calibration_addon(coefficients, sets["calibration"])
        models[kind] = coefficients, addon
        trained[kind] = {
            "ridge_coefficients": [round(value, 12) for value in coefficients],
            "calibration_residual_addon_factor": round(addon, 12),
            "calibration_rank_one_based": rank,
            "fit_n": len(sets["fit"]),
            "fit_issue_days": days["fit"],
            "calibration_n": len(sets["calibration"]),
            "calibration_issue_days": days["calibration"],
            "test": metrics(coefficients, addon, sets["test"]),
        }
    outlooks = []
    for row in sensitivity["outlooks"]:
        issued, target = (stamp(row[k]) for k in ("forecast_issued_at", "forecast_valid_at"))
        lead = (target - issued).total_seconds() / 3600.0
        if not min(LEADS) <= lead <= max(LEADS):
            raise ValueError(f"Replay forecast lead {lead}h falls outside the sampled lead range")
        result = {key: row[key] for key in ("forecast_issued_at", "forecast_valid_at")}
        for kind, (coefficients, addon) in models.items():
            point = row[f"forecast_{kind}_factor"]
            if point is None:
                raise ValueError(f"Missing replay {kind} point factor for {target.isoformat()}")
            result[f"{kind}_scenario_factor_range"] = row[f"{kind}_scenario_factor_range"]
            result[f"{kind}_margin_factor"] = round(margin(coefficients, addon, point, lead), 10)
        outlooks.append(result)
    return {
        "schema_version": 1,
        "source_pack_sha256": pack_hash,
        "training_data_sha256": sha256(training_bytes).hexdigest(),
        "method": {
            "id": "hrrr-analysis-ridge-abs-error-v1",
            "target_label": "NOAA HRRR weather-model analysis for the same hour, not measured generation",
            "features": ["intercept", "point_factor", "point_factor_squared", "lead_hours_divided_by_13"],
            "ridge_penalty": RIDGE_PENALTY,
            "ridge_penalty_excludes_intercept": True,
            "residual_calibration_target": CALIBRATION_TARGET,
            "training_forecast_issue_hour_utc": training["forecast_cycle_utc"],
            "training_sample_lead_hours": list(LEADS),
            "chronological_split_issue_dates_utc": {
                name: [first.isoformat(), last.isoformat()] for name, (first, last) in SPLITS.items()
            },
            "first_replay_issue_utc": earliest.isoformat(),
            "unavailable_public_products": len(training["unavailable_products"]),
            "observational_coverage_claim": False,
        },
        "source": sensitivity["source"],
        "scenario_assumptions": sensitivity["method"],
        "models": trained,
        "outlooks": outlooks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, default=PACK)
    parser.add_argument("--training", type=Path, default=TRAINING)
    parser.add_argument("--output", type=Path, default=ARTIFACT)
    args = parser.parse_args()
    if args.output.resolve() in (args.pack.resolve(), args.training.resolve()):
        parser.error("the output must not overwrite either input")
    try:
        result = generate(args.pack, args.training)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    print(f"Prepared {len(result['outlooks'])} Austin target forecasts at {args.output}")
    print(json.dumps(result["models"], indent=2))


if __name__ == "__main__":
    main()
