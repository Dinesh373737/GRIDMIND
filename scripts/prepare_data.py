"""Prepare GRIDMIND training/replay inputs from bounded historical downloads.

The checked-in public pack runs without downloads. The optional measured
meter and market-pilot inputs are prepared under .runtime/datasets.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO, TextIOWrapper
import json
import math
from pathlib import Path
from statistics import mean, quantiles
import sys
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gridmind.datasets import DATA, LOCAL_DATA, solar_yield, weather_condition, wind_yield


UTC = timezone.utc
HOUR = timedelta(hours=1)
SLOT = timedelta(minutes=15)
TRAIN_START = datetime(2018, 8, 23, tzinfo=UTC)
TRAIN_END = datetime(2018, 8, 24, tzinfo=UTC)
REPLAY_START = datetime(2018, 8, 24, 18, tzinfo=UTC)
REPLAY_END = datetime(2018, 8, 26, tzinfo=UTC)
SIGNAL_START = datetime(2018, 8, 24, 20, tzinfo=UTC)
SIGNAL_END = datetime(2018, 8, 24, 21, 15, tzinfo=UTC)

EIA = "https://www.eia.gov/electricity/gridmonitor/sixMonthFiles/EIA930_BALANCE_2018_Jul_Dec.csv"
EAGLE = "https://ndownloader.figshare.com/files/42547879"
EAGLE_2018_BYTES = 999210542
ZARR = "https://hrrrzarr.s3.amazonaws.com"
GRIB = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
KAGGLE = (
    "https://www.kaggle.com/api/v1/datasets/download/"
    "pecanstreetinc/ps-data-10-homes-1min-frequency-aug-2018"
)
UGIM = "https://zenodo.org/records/3371222/files/data_03062019_09062019_v2.xlsx?download=1"
GRIB_DECODER_LOCK = Lock()


def at_intervals(start: datetime, end: datetime, interval: timedelta):
    while start < end:
        yield start
        start += interval


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_floor(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def slot_floor(value: datetime) -> datetime:
    return value.replace(minute=value.minute // 15 * 15, second=0, microsecond=0)


def request_bytes(url: str, limit: int, *, start: int | None = None) -> tuple[bytes, str]:
    headers = {"User-Agent": "GRIDMIND historical replay builder"}
    if start is not None:
        headers["Range"] = f"bytes={start}-{start + limit - 1}"
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                if start is not None and (
                    response.status != 206
                    or not response.headers.get("Content-Range", "").startswith(f"bytes {start}-")
                ):
                    raise ValueError("EAGLE-I server did not return the requested byte range")
                data = response.read(limit + (start is None))
                if len(data) > limit:
                    raise ValueError("Source file exceeds the configured download limit")
                return data, response.geturl()
        except HTTPError as exc:
            if exc.code < 500 or attempt == 2:
                raise
        except (TimeoutError, URLError):
            if attempt == 2:
                raise
    raise RuntimeError("Download retries exhausted")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    staging.replace(path)


def read_eia() -> dict[datetime, float]:
    """Stream the published half-year CSV and keep only the matching ERCOT hours."""
    result = {}
    with urlopen(EIA, timeout=30) as response, TextIOWrapper(response, encoding="utf-8-sig") as stream:
        in_ercot = False
        for row in csv.DictReader(stream):
            if row["Balancing Authority"] != "ERCO":
                if in_ercot:
                    break
                continue
            in_ercot = True
            timestamp = datetime.strptime(row["UTC Time at End of Hour"], "%m/%d/%Y %I:%M:%S %p")
            timestamp = timestamp.replace(tzinfo=UTC)
            if timestamp >= REPLAY_END:
                break
            demand = row["Demand (MW)"]
            if timestamp >= TRAIN_START and demand:
                result[timestamp] = float(demand)
    required = set(at_intervals(TRAIN_START, REPLAY_END, HOUR))
    if required - result.keys():
        raise ValueError("EIA archive lacks one or more matching demand observations")
    return result


def read_eagle() -> dict[datetime, int | None]:
    """Locate a narrow date slice in the timestamp-sorted county archive."""
    _, link = request_bytes(EAGLE, 2048, start=0)
    target = REPLAY_START.strftime("%Y-%m-%d %H:%M:%S").encode()
    left, right = 1, EAGLE_2018_BYTES - 2048
    while right - left > 125_000:
        offset = (left + right) // 2
        sample, _ = request_bytes(link, 2048, start=offset)
        second_line = sample.splitlines()[1]
        if second_line.rsplit(b",", 1)[-1] < target:
            left = offset
        else:
            right = offset
    chunk, _ = request_bytes(link, 8_000_000, start=left)
    lines = chunk.splitlines()[1:-1]
    if lines[-1].rsplit(b",", 1)[-1].decode() < REPLAY_END.strftime("%Y-%m-%d %H:%M:%S"):
        raise ValueError("County outage sample ends before the end of the replay")

    result = {}
    for line in lines:
        if not line.startswith(b"48453,"):
            continue
        _, _, _, count, raw_timestamp = line.decode().split(",")
        timestamp = datetime.fromisoformat(raw_timestamp).replace(tzinfo=UTC)
        if REPLAY_START <= timestamp < REPLAY_END:
            result[timestamp] = int(count) if count else None
    for timestamp in at_intervals(SIGNAL_START, SIGNAL_END, SLOT):
        if result.get(timestamp) is None or result[timestamp] < 300:
            raise ValueError("The confirmed county outage window changed or has a data gap")
    if result.get(SIGNAL_END) is None or result[SIGNAL_END] >= 300:
        raise ValueError("The county series lacks the selected recovery observation")
    return result


def read_irradiance(issue: datetime, lead: int, latitude: float, longitude: float) -> float:
    """Read an original GRIB message where the companion Zarr archive has metadata only."""
    from eccodes import codes_grib_find_nearest, codes_new_from_message, codes_release

    url = f"{GRIB}/hrrr.{issue:%Y%m%d}/conus/hrrr.t{issue:%H}z.wrfsfcf{lead:02d}.grib2"
    index = request_bytes(url + ".idx", 70_000)[0].decode().splitlines()
    for pos, field in enumerate(index[:-1]):
        if ":DSWRF:surface:" in field:
            start = int(field.split(":")[1])
            stop = int(index[pos + 1].split(":")[1])
            message = request_bytes(url, stop - start, start=start)[0]
            # The decoder uses a shared native context on Windows.
            with GRIB_DECODER_LOCK:
                handle = codes_new_from_message(message)
                try:
                    return float(codes_grib_find_nearest(handle, latitude, longitude)[0]["value"])
                finally:
                    codes_release(handle)
    raise ValueError(f"Archived irradiance is missing for {iso(issue)} lead {lead}")


def read_weather() -> tuple[dict[datetime, dict], dict[datetime, dict], dict]:
    """Decode one grid point from each needed historical HRRR forecast cycle."""
    import numpy as np
    from numcodecs.blosc import decompress
    from pyproj import Proj

    def axis(dimension: str):
        key = f"{ZARR}/grid/HRRR_chunk_index.zarr/{dimension}/0"
        return np.frombuffer(decompress(request_bytes(key, 100_000)[0]), dtype="<f8")

    latitude, longitude = 30.2672, -97.7431
    project = Proj(proj="lcc", a=6371229, b=6371229, lon_0=262.5,
                   lat_0=38.5, lat_1=38.5, lat_2=38.5)
    projected_x, projected_y = project(longitude, latitude)
    axes_x, axes_y = axis("x"), axis("y")
    grid_x = int(np.argmin(abs(axes_x - projected_x)))
    grid_y = int(np.argmin(abs(axes_y - projected_y)))
    chunk_x, x = divmod(grid_x, 150)
    chunk_y, y = divmod(grid_y, 150)

    fields = {
        "cloud_pct": ("entire_atmosphere", "TCDC"),
        "rain_mm": ("surface", "APCP_1hr_acc_fcst"),
        "temperature_k": ("2m_above_ground", "TMP"),
        "wind_u": ("10m_above_ground", "UGRD"),
        "wind_v": ("10m_above_ground", "VGRD"),
        "hub_wind_u": ("80m_above_ground", "UGRD"),
        "hub_wind_v": ("80m_above_ground", "VGRD"),
        "gust_ms": ("surface", "GUST"),
    }
    cycles = tuple(at_intervals(TRAIN_START, REPLAY_END, 12 * HOUR))

    def decode(item):
        issue, (name, (level, variable)) = item
        prefix = issue.strftime("%Y%m%d/%Y%m%d_%Hz_fcst.zarr")
        source = f"{ZARR}/sfc/{prefix}/{level}/{variable}/{level}/{variable}/0.{chunk_y}.{chunk_x}"
        try:
            compressed, _ = request_bytes(source, 3_000_000)
        except HTTPError as exc:
            raise ValueError(f"Unable to read archived HRRR {name} at {iso(issue)}: {source}") from exc
        points = np.frombuffer(decompress(compressed), dtype="<f2").reshape(-1, 150, 150)
        return issue, name, points[:, y, x].astype(float)

    forecasts = defaultdict(dict)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for issue, field, values in pool.map(decode, [
            (issue, field) for issue in cycles for field in fields.items()
        ]):
            forecasts[issue][field] = values

    hours = tuple(at_intervals(TRAIN_START + HOUR, REPLAY_END, HOUR))
    issue_for_hour = {hour: max(cycle for cycle in cycles if cycle <= hour - HOUR) for hour in hours}
    irradiance_keys = sorted({
        (issue, int((t + delta - issue) / HOUR))
        for t, issue in issue_for_hour.items()
        for delta in (timedelta(), HOUR)
    })
    with ThreadPoolExecutor(max_workers=12) as pool:
        irradiance = dict(zip(irradiance_keys, pool.map(
            lambda item: read_irradiance(*item, latitude, longitude), irradiance_keys
        )))

    def weather_at(as_of: datetime, target: datetime) -> dict:
        issue = issue_for_hour[as_of]
        lead = int((target - issue) / HOUR)
        values = {key: float(array[lead - 1]) for key, array in forecasts[issue].items()}
        if not all(math.isfinite(value) and value != -10000.0 for value in values.values()):
            raise ValueError(f"Missing forecast field at {iso(target)}")
        temperature = values["temperature_k"] - 273.15
        watts = max(0.0, irradiance[issue, lead])
        wind = math.hypot(values["wind_u"], values["wind_v"])
        hub_wind = math.hypot(values["hub_wind_u"], values["hub_wind_v"])
        return {
            "cloud_pct": round(values["cloud_pct"], 2),
            "rain_mm": round(max(0.0, values["rain_mm"]), 3),
            "temperature_c": round(temperature, 2),
            "wind_ms": round(wind, 3),
            "gust_ms": round(values["gust_ms"], 3),
            "irradiance_wm2": round(watts, 2),
            "solar_factor": round(solar_yield(watts, temperature), 5),
            "wind_factor": round(wind_yield(hub_wind), 5),
            "issued_at": iso(issue),
            "valid_at": iso(target),
        }

    current, ahead = {}, {}
    # The previous day's cycles lack complete point data. Start the weather
    # calibration when the first cycle is available after its publication lag.
    for hour in hours:
        current[hour] = weather_at(hour, hour)
        ahead[hour] = weather_at(hour, hour + HOUR)
    return current, ahead, {"latitude": latitude, "longitude": longitude,
                            "x_index": grid_x, "y_index": grid_y}


def empirical_growth(series: dict[datetime, float], interval: timedelta) -> list[float]:
    return [
        round(series[t + HOUR] / series[t], 5)
        for t in at_intervals(TRAIN_START, TRAIN_END - HOUR, interval)
        if t in series and t + HOUR in series and series[t] > 0
    ]


def run_refresh_public() -> None:
    with ThreadPoolExecutor(max_workers=3) as downloads:
        eia_task = downloads.submit(read_eia)
        outage_task = downloads.submit(read_eagle)
        weather_task = downloads.submit(read_weather)
        eia, county = eia_task.result(), outage_task.result()
        weather, outlook, gridpoint = weather_task.result()

    reference_mw = max(v for t, v in eia.items() if t < TRAIN_END)
    ticks = []
    for timestamp in at_intervals(REPLAY_START, REPLAY_END, SLOT):
        hour = hour_floor(timestamp)
        conditions, upcoming = weather[hour], outlook[hour]
        regional_load = eia[hour]
        ticks.append({
            "timestamp": iso(timestamp),
            "demand_kw": max(0.0, round(regional_load / reference_mw * 100, 4)),
            "solar_kw": round(conditions["solar_factor"] * 30, 4),
            "load_factor": round(min(1.2, regional_load / reference_mw), 5),
            "solar_factor": conditions["solar_factor"],
            "wind_factor": conditions["wind_factor"],
            "forecast_solar_factor": upcoming["solar_factor"],
            "forecast_wind_factor": upcoming["wind_factor"],
            "cloud_pct": conditions["cloud_pct"],
            "rain_mm": conditions["rain_mm"],
            "temperature_c": conditions["temperature_c"],
            "wind_ms": conditions["wind_ms"],
            "gust_ms": conditions["gust_ms"],
            "irradiance_wm2": conditions["irradiance_wm2"],
            "forecast_issued_at": conditions["issued_at"],
            "forecast_valid_at": conditions["valid_at"],
            "next_forecast_issued_at": upcoming["issued_at"],
            "next_forecast_valid_at": upcoming["valid_at"],
            "outlook_condition": weather_condition(
                upcoming["cloud_pct"], upcoming["rain_mm"], upcoming["wind_ms"],
                upcoming["irradiance_wm2"],
            ),
            "county_customers_out": county.get(timestamp),
            "regional_demand_mw": regional_load,
            "outage": SIGNAL_START <= timestamp < SIGNAL_END,
        })
    payload = {
        "manifest": {
            "id": "austin",
            "revision": 1,
            "eagle_county_fips": "48453",
            "regional_demand_source": EIA,
            "outage_source": "https://doi.org/10.6084/m9.figshare.24237376.v4",
            "outage_data_license": "CC BY 4.0",
            "weather_source": "https://registry.opendata.aws/noaa-hrrr-pds/",
            "weather_archive": "University of Utah HRRR Zarr and original NOAA GRIB2 irradiance",
            "weather_gridpoint": gridpoint,
            "weather_publication_delay_minutes": 60,
            "data_kind": "regional_demand_proxy",
            "calibration_start": iso(TRAIN_START),
            "calibration_end_exclusive": iso(TRAIN_END),
            "scenario_outage_start": iso(SIGNAL_START),
            "scenario_restored_at": iso(SIGNAL_END),
            "scenario_outage_threshold": 300,
        },
        "demand_error_factors": empirical_growth(eia, HOUR),
        "calibration_solar_factors": {
            iso(t): weather[t]["solar_factor"]
            for t in at_intervals(TRAIN_START + HOUR, TRAIN_END, HOUR)
        },
        "ticks": ticks,
    }
    write_json(DATA / "austin_public.json", payload)
    print(f"Prepared {len(ticks)} checked-in Austin replay steps.")


def read_meters() -> tuple[dict[datetime, float], dict[datetime, float], str]:
    """Aggregate the publisher's minute samples by home, then across homes."""
    content, _ = request_bytes(KAGGLE, 8_000_000)
    sample_sum = defaultdict(float)
    sample_count = defaultdict(int)
    with ZipFile(BytesIO(content)) as archive, archive.open("PecanStreet_10_Homes_1Min_Data.csv") as raw:
        rows = csv.DictReader(TextIOWrapper(raw, encoding="utf-8-sig"))
        for row in rows:
            if row["Measure"] != "Real Power" or row["Circuit"] not in {"Main Panel", "Solar"}:
                continue
            try:
                value = float(row["Value"])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            timestamp = datetime.fromisoformat(row["Datetime (UTC)"]).astimezone(UTC)
            if TRAIN_START <= timestamp < REPLAY_END:
                key = (slot_floor(timestamp), row["Home ID"], row["Circuit"])
                sample_sum[key] += value
                sample_count[key] += 1
    load, solar = defaultdict(float), defaultdict(float)
    for (timestamp, home, circuit), sample_total in sample_sum.items():
        count = sample_count[(timestamp, home, circuit)]
        if count >= 12:
            output = load if circuit == "Main Panel" else solar
            output[timestamp] += max(0.0, sample_total / count)
    return dict(load), dict(solar), sha256(content).hexdigest()


def run_meter_overlay() -> None:
    with (DATA / "austin_public.json").open(encoding="utf-8") as handle:
        public = json.load(handle)
    load, output, checksum = read_meters()
    reference_load = max(v for t, v in load.items() if t < TRAIN_END)
    reference_output = max(v for t, v in output.items() if t < TRAIN_END)

    calibration_days = []
    for hour in at_intervals(TRAIN_START + HOUR, TRAIN_END, HOUR):
        values = [output[t] / reference_output for t in at_intervals(hour, hour + HOUR, SLOT) if t in output]
        modeled = public["calibration_solar_factors"][iso(hour)]
        if values and modeled > 0.09:
            calibration_days.append((modeled, mean(values)))
    coefficient = (
        sum(x * y for x, y in calibration_days) / sum(x * x for x, _ in calibration_days)
    )
    errors = [y - x * coefficient for x, y in calibration_days]
    boundaries = quantiles(errors, n=10, method="inclusive")

    merged = {}
    for row in public["ticks"]:
        stamp = row["timestamp"]
        timestamp = datetime.fromisoformat(stamp)
        if timestamp not in load or timestamp not in output:
            continue
        forecast = min(1.0, row["forecast_solar_factor"] * coefficient)
        interval = [0.0, 0.0] if forecast == 0 else [
            round(max(0.0, min(1.0, forecast + e)), 5) for e in (boundaries[0], boundaries[-1])
        ]
        merged[stamp] = {
            "demand_kw": round(load[timestamp], 4),
            "solar_kw": round(output[timestamp], 4),
            "load_factor": round(min(1.2, load[timestamp] / reference_load), 5),
            "solar_factor": round(min(1.0, output[timestamp] / reference_output), 5),
            "forecast_solar_factor": round(forecast, 5),
            "forecast_solar_range": interval,
            "data_kind": "meter_profile",
        }

    write_json(LOCAL_DATA / "austin_meter_overlay.json", {
        "manifest": {
            "source": "https://www.pecanstreet.org/2025/07/public-data/",
            "archive_sha256": checksum,
            "data_kind": "meter_profile",
            "calibration_pairs": len(calibration_days),
            "calibration": "least-squares scale and empirical residual band",
        },
        "ticks": merged,
        "demand_error_factors": empirical_growth(load, SLOT),
    })
    print(f"Prepared {len(merged)} local measured-meter steps for Austin.")


def run_ugim_benchmark() -> None:
    """Extract the office pilot's hourly profiles and its logged market fills."""
    import re

    content, _ = request_bytes(UGIM, 1_500_000)
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    records, trades = {}, {}
    conflicting_hours = set()
    with ZipFile(BytesIO(content)) as archive:
        metadata = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        strings = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = ["".join(node.itertext()).strip() for node in strings]
        for number, sheet in enumerate(metadata.findall(".//s:sheet", namespace), start=1):
            name = sheet.attrib["name"]
            matched = re.fullmatch(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)_(Z\.0|L\.[123]|R\.2)(_p2p)?", name)
            if not matched:
                continue
            day, participant, trades_sheet = matched.groups()
            contents = ElementTree.fromstring(archive.read(f"xl/worksheets/sheet{number}.xml"))
            for row in contents.findall(".//s:row", namespace):
                cells = {}
                for cell in row.findall("s:c", namespace):
                    value = cell.findtext("s:v", namespaces=namespace)
                    if value is not None:
                        column = cell.attrib["r"].rstrip("0123456789")
                        cells[column] = shared[int(value)] if cell.get("t") == "s" else value
                if not cells.get("A", "").isdigit():
                    continue
                if trades_sheet and cells.keys() >= set("ABCDEFGHI"):
                    timestamp = cells["G"]
                    seller, buyer = cells["C"], cells["H"]
                    trades[(timestamp, seller, buyer, cells["I"], cells["D"])] = {
                        "day": day,
                        "excel_timestamp": float(timestamp),
                        "seller": seller,
                        "buyer": buyer,
                        "quantity_kwh": float(cells["I"]) / 1000,
                        "price_eur_per_kwh": float(cells["D"]),
                    }
                elif not trades_sheet and int(cells["A"]) < 24 and cells.keys() >= set("ABCDEFGHI"):
                    hour = int(cells["A"])
                    key = day, participant, hour
                    record = {
                        "day": day, "hour_local": hour, "participant": participant,
                        "planned_sell_kwh": float(cells["B"]) / 1000,
                        "planned_buy_kwh": float(cells["C"]) / 1000,
                        "forecast_demand_kwh": float(cells["F"]) / 1000,
                        "forecast_generation_kwh": float(cells["G"]) / 1000,
                        "observed_demand_kwh": float(cells["H"]) / 1000,
                        "observed_generation_kwh": float(cells["I"]) / 1000,
                    }
                    if key in conflicting_hours:
                        continue
                    if key in records and records[key] != record:
                        conflicting_hours.add(key)
                        del records[key]
                    else:
                        records[key] = record
    write_json(LOCAL_DATA / "ugim_benchmark.json", {
        "manifest": {
            "source": "https://zenodo.org/records/3371222",
            "license": "CC BY-NC-ND 4.0",
            "archive_sha256": sha256(content).hexdigest(),
            "conflicting_hours": sorted(conflicting_hours),
        },
        "hourly_participant_records": list(records.values()),
        "recorded_trades": list(trades.values()),
    })
    print(f"Prepared µGIM benchmark: {len(records)} source-hour rows, {len(trades)} distinct fills.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-public", action="store_true", help="Rebuild checked-in Austin archive")
    parser.add_argument("--local-profiles", action="store_true", help="Prepare local Austin meters and µGIM")
    args = parser.parse_args()
    if args.refresh_public:
        run_refresh_public()
    if args.local_profiles:
        run_meter_overlay()
        run_ugim_benchmark()
    if not args.refresh_public and not args.local_profiles:
        parser.print_help()


if __name__ == "__main__":
    main()
