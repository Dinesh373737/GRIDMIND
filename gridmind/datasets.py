"""Offline replay and forecast inputs for locality workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from random import Random


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOCAL_DATA = ROOT / ".runtime" / "datasets"
PACK = "austin"


@dataclass(frozen=True)
class ReplayTick:
    timestamp: str
    demand_kw: float
    solar_kw: float
    solar_factor: float
    load_factor: float
    wind_factor: float
    forecast_solar_factor: float
    forecast_wind_factor: float
    cloud_pct: float
    rain_mm: float
    temperature_c: float
    wind_ms: float
    gust_ms: float
    irradiance_wm2: float
    forecast_issued_at: str
    forecast_valid_at: str
    next_forecast_issued_at: str
    next_forecast_valid_at: str
    county_customers_out: int | None
    regional_demand_mw: float
    outage: bool
    outlook_condition: str | None = None
    data_kind: str = "regional_demand_proxy"
    forecast_solar_range: tuple[float, float] | None = None
    renewable_outlook: dict | None = None


def solar_yield(irradiance_wm2: float, temperature_c: float) -> float:
    """Estimate the fraction of rated PV output under forecast irradiance."""
    cell_temperature = temperature_c + 0.028 * irradiance_wm2
    temperature_derate = 1 - 0.004 * (cell_temperature - 25.0)
    return max(0.0, min(1.0, irradiance_wm2 / 1000.0 * temperature_derate))


def wind_yield(wind_ms: float) -> float:
    """Idealized turbine curve for the replay's virtual assets."""
    if wind_ms < 3.0 or wind_ms >= 25.0:
        return 0.0
    return min(1.0, (wind_ms ** 3 - 3.0 ** 3) / (12.0 ** 3 - 3.0 ** 3))


def weather_condition(cloud_pct: float, rain_mm: float, wind_ms: float,
                      irradiance_wm2: float) -> str:
    if rain_mm >= 0.2:
        return "Rainy"
    if wind_ms >= 8.0:
        return "Windy"
    if cloud_pct >= 60:
        return "Cloudy"
    if irradiance_wm2 >= 200 and cloud_pct <= 30:
        return "Sunny"
    if cloud_pct <= 30:
        return "Clear"
    return "Partly cloudy"


@lru_cache(maxsize=4)
def _read(path: Path, modified_ns: int) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _pack(path: Path) -> dict:
    return _read(path, path.stat().st_mtime_ns)


def _public(pack: str) -> dict:
    if pack != PACK:
        raise ValueError(f"Unknown replay pack: {pack}")
    return _pack(DATA / "austin_public.json")


def _meter_overlay(pack: str) -> dict | None:
    path = LOCAL_DATA / "austin_meter_overlay.json"
    if pack == PACK and path.is_file():
        return _pack(path)
    return None


def _outlook_time(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Renewable outlook dates must be ISO UTC timestamps") from error
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("Renewable outlook dates must be ISO UTC timestamps")
    return stamp


def _outlook_key(row: dict, prefix: str = "") -> tuple[datetime, datetime]:
    return (_outlook_time(row.get(f"{prefix}forecast_issued_at")),
            _outlook_time(row.get(f"{prefix}forecast_valid_at")))


def _unit_factor(value: object, name: str) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 1:
        raise ValueError(f"Renewable outlook {name} must be a finite number from 0 to 1")
    return float(value)


@lru_cache(maxsize=4)
def _read_renewable_outlook(source_path: Path, source_mtime: int,
                            artifact_path: Path, artifact_mtime: int) -> tuple[dict, dict]:
    # Older Windows worktrees can still have CRLF after an update. Verify the
    # canonical LF bytes without accepting any change to the JSON content.
    source_raw = source_path.read_bytes().replace(b"\r\n", b"\n")
    artifact_raw = artifact_path.read_bytes().replace(b"\r\n", b"\n")
    artifact = json.loads(artifact_raw)
    if not isinstance(artifact, dict) or type(artifact.get("schema_version")) is not int or artifact["schema_version"] != 1:
        raise ValueError("Unknown Austin renewable outlook schema version")
    source_sha256 = sha256(source_raw).hexdigest()
    if artifact.get("source_pack_sha256") != source_sha256:
        raise ValueError("Austin renewable outlook source hash does not match austin_public.json")
    method = artifact.get("method")
    if not isinstance(method, dict) or not all(isinstance(method.get(k), str) and method[k].strip()
                                               for k in ("id", "target_label")):
        raise ValueError("Austin renewable outlook method requires an id and target label")
    manifest = {
        "schema_version": 1,
        "source_pack_sha256": source_sha256,
        "artifact_sha256": sha256(artifact_raw).hexdigest(),
        "method": {k: method[k].strip() for k in ("id", "target_label")},
    }
    entries = artifact.get("outlooks")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Austin renewable outlook requires an array of outlooks")
    points = {_outlook_key(row, "next_"): row for row in json.loads(source_raw)["ticks"]}
    by_key = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Austin renewable outlook entries must be objects")
        key = issued, valid = _outlook_key(entry)
        if issued >= valid or key in by_key:
            raise ValueError("Austin renewable outlook requires unique issue/target pairs with issuance before target")
        outlook = {"method_id": manifest["method"]["id"], "target_label": manifest["method"]["target_label"]}
        source = points.get(key)
        for kind in ("solar", "wind"):
            band_name, margin_name = f"{kind}_scenario_factor_range", f"{kind}_margin_factor"
            if band_name not in entry or margin_name not in entry:
                raise ValueError(f"Austin renewable outlook requires both {band_name} and {margin_name}")
            band, margin = entry[band_name], entry[margin_name]
            if band is not None:
                if not isinstance(band, list) or len(band) != 2:
                    raise ValueError(f"Austin renewable outlook {band_name} must have two bounds")
                low, high = (_unit_factor(bound, band_name) for bound in band)
                point = source.get(f"forecast_{kind}_factor") if source is not None else None
                if low > high or source is not None and (point is None or not low <= point <= high):
                    raise ValueError(f"Austin renewable outlook {band_name} must enclose the archived forecast")
                band = (low, high)
            outlook[band_name] = band
            outlook[margin_name] = None if margin is None else _unit_factor(margin, margin_name)
        by_key[key] = outlook
    return manifest, by_key


def _renewable_outlook(pack: str) -> tuple[dict, dict] | None:
    if pack != PACK:
        return None
    artifact = DATA / "austin_renewable_ml.json"
    if not artifact.is_file():
        return None
    source = DATA / "austin_public.json"
    return _read_renewable_outlook(source, source.stat().st_mtime_ns, artifact, artifact.stat().st_mtime_ns)


def replay_manifest(pack: str = PACK) -> dict:
    public = _public(pack)
    manifest = dict(public["manifest"])
    overlay = _meter_overlay(pack)
    if overlay:
        manifest["local_meter_overlay"] = overlay["manifest"]
    renewable_outlook = _renewable_outlook(pack)
    if renewable_outlook:
        manifest["renewable_outlook"] = renewable_outlook[0]
    return manifest


def replay_options() -> list[dict]:
    pack = _public(PACK)
    return [{
        "id": PACK,
        "name": "Austin / Travis County",
        "start_at": pack["ticks"][0]["timestamp"],
        "end_at": pack["ticks"][-1]["timestamp"],
        "steps": len(pack["ticks"]),
        "step_minutes": 15,
    }]


def load_replay(pack: str = PACK) -> tuple[ReplayTick, ...]:
    public = _public(pack)
    overlay = _meter_overlay(pack)
    meter_rows = overlay["ticks"] if overlay else {}
    renewable_outlook = _renewable_outlook(pack)
    outlooks = renewable_outlook[1] if renewable_outlook else {}
    ticks = []
    for row in public["ticks"]:
        meter_row = meter_rows.get(row["timestamp"], {})
        # The local Pecan band describes a different solar point and target.
        # Do not combine it with archived weather scenario / error factors.
        outlook = outlooks.get(_outlook_key(row, "next_")) if outlooks and not meter_row else None
        ticks.append(ReplayTick(**(row | meter_row), renewable_outlook=outlook.copy() if outlook else None))
    return tuple(ticks)


def load_ugim_benchmark() -> dict | None:
    path = LOCAL_DATA / "ugim_benchmark.json"
    return _pack(path) if path.is_file() else None


def pack_demand_errors(ticks: tuple[ReplayTick, ...], pack: str = PACK) -> tuple[float, ...]:
    """Use only the preparation window that ends before the first replay step."""
    overlay = _meter_overlay(pack) if ticks and ticks[0].data_kind == "meter_profile" else None
    return tuple((overlay or _public(pack))["demand_error_factors"])


def engine_frame(ticks: tuple[ReplayTick, ...], offset: int, *,
                 demand_errors: tuple[float, ...] | None = None) -> dict:
    row = ticks[offset]
    weather = {
        "condition": weather_condition(row.cloud_pct, row.rain_mm, row.wind_ms, row.irradiance_wm2),
        "cloud_pct": row.cloud_pct,
        "rain_mm": row.rain_mm,
        "temperature_c": row.temperature_c,
        "wind_ms": row.wind_ms,
        "gust_ms": row.gust_ms,
        "irradiance_wm2": row.irradiance_wm2,
        "issued_at": row.forecast_issued_at,
        "valid_at": row.forecast_valid_at,
        "outlook_issued_at": row.next_forecast_issued_at,
        "outlook_valid_at": row.next_forecast_valid_at,
        "outlook_condition": row.outlook_condition,
    }
    measurements = {
        "regional_demand_mw": row.regional_demand_mw,
        "county_customers_out": row.county_customers_out,
    }
    if row.data_kind == "meter_profile":
        measurements.update(austin_meter_demand_kw=row.demand_kw, austin_meter_solar_kw=row.solar_kw)
    frame = {
        "step": offset,
        "timestamp": row.timestamp,
        "load_factor": row.load_factor,
        "solar_factor": row.solar_factor,
        "wind_factor": row.wind_factor,
        "outage": row.outage if row.county_customers_out is not None else None,
        "forecast_solar_factor": row.forecast_solar_factor,
        "forecast_wind_factor": row.forecast_wind_factor,
        "forecast_solar_range": row.forecast_solar_range,
        "demand_error_factors": demand_errors if demand_errors is not None else pack_demand_errors(ticks),
        "data_kind": row.data_kind,
        "weather": weather,
        "measurements": measurements,
    }
    if row.renewable_outlook is not None:
        frame["renewable_outlook"] = row.renewable_outlook
    return frame


@dataclass(frozen=True)
class WeatherRegime:
    label: str
    transmission: float = 1.0
    min_cloud: float = 0.0
    min_rain: float = 0.0
    min_wind: float = 0.0
    min_hub_wind: float = 0.0
    cooling: float = 0.0


def _weather_regime(minute: float, seed: int) -> WeatherRegime:
    block = int(minute // 90)
    intensity = Random(f"gridmind:weather:{seed}:{block}").random()
    return (
        WeatherRegime("Archive"),
        WeatherRegime("Cloudy", 0.40 + 0.20 * intensity, 75.0, cooling=2.0),
        WeatherRegime("Rainy", 0.14 + 0.18 * intensity, 92.0, 2.0 + 5 * intensity,
                      min_wind=5.0, min_hub_wind=7.0, cooling=4.0),
        WeatherRegime("Windy", min_wind=9.0 + 3.0 * intensity,
                      min_hub_wind=11.0 + 3.5 * intensity),
    )[block % 4]


def _transmit_solar(factor: float, regime: WeatherRegime) -> float:
    thermal_adjustment = 1.0 + 0.004 * regime.cooling
    return round(min(1.0, factor * regime.transmission * thermal_adjustment), 5)


def augment_weather(frame: dict, seed: int, interval_minutes: int) -> dict:
    """Apply a repeatable weather stress scenario without rewriting source observations."""
    if interval_minutes <= 0:
        raise ValueError("Weather scenario step duration must be positive")
    weather = frame["weather"]
    minute = int(frame["step"]) * interval_minutes
    current = _weather_regime(minute, seed)
    horizon = 60.0
    if weather.get("outlook_valid_at"):
        at = datetime.fromisoformat(frame["timestamp"])
        valid_at = datetime.fromisoformat(weather["outlook_valid_at"])
        horizon = max(0.0, (valid_at - at).total_seconds() / 60.0)
    outlook = _weather_regime(minute + horizon, seed)
    archive_factors = {
        key: frame.get(key)
        for key in ("solar_factor", "wind_factor", "forecast_solar_factor", "forecast_wind_factor",
                    "forecast_solar_range")
    }
    result = {**frame, "weather": dict(weather)}
    result["provenance"] = {
        **(frame.get("provenance") or {}),
        "weather_scenario": {
            "method": "seeded weather blocks from archived conditions",
            "seed": seed,
            "current_regime": current.label,
            "outlook_regime": outlook.label,
            "original_weather": dict(weather),
            "original_factors": archive_factors,
        },
    }
    modeled = result["weather"]
    if current.label != "Archive":
        wind = max(weather["wind_ms"], current.min_wind)
        modeled.update(
            condition=current.label,
            cloud_pct=max(weather["cloud_pct"], current.min_cloud),
            rain_mm=max(weather["rain_mm"], current.min_rain),
            temperature_c=weather["temperature_c"] - current.cooling,
            wind_ms=round(wind, 3),
            gust_ms=round(max(weather["gust_ms"], wind * 1.32), 3),
            irradiance_wm2=round(weather["irradiance_wm2"] * current.transmission, 2),
            issued_at=None,
            source_kind="synthetic",
        )
        result["solar_factor"] = _transmit_solar(frame["solar_factor"], current)
        result["wind_factor"] = round(max(frame["wind_factor"], wind_yield(current.min_hub_wind)), 5)
    if outlook.label != "Archive":
        modeled.update(outlook_condition=outlook.label, outlook_issued_at=None,
                       outlook_source_kind="synthetic")
        result.pop("renewable_outlook", None)
        if frame.get("forecast_solar_factor") is not None:
            result["forecast_solar_factor"] = _transmit_solar(frame["forecast_solar_factor"], outlook)
        if frame.get("forecast_wind_factor") is not None:
            result["forecast_wind_factor"] = round(max(
                frame["forecast_wind_factor"], wind_yield(outlook.min_hub_wind)
            ), 5)
        if frame.get("forecast_solar_range") is not None:
            result["forecast_solar_range"] = tuple(
                _transmit_solar(value, outlook) for value in frame["forecast_solar_range"]
            )
    return result
