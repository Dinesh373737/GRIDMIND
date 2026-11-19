"""Offline replay and forecast inputs for locality workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
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


def replay_manifest(pack: str = PACK) -> dict:
    public = _public(pack)
    manifest = dict(public["manifest"])
    overlay = _meter_overlay(pack)
    if overlay:
        manifest["local_meter_overlay"] = overlay["manifest"]
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
    return tuple(
        ReplayTick(**(row | meter_rows.get(row["timestamp"], {})))
        for row in public["ticks"]
    )


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
    return {
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
