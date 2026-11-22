"""Fetch the fixed public NOAA HRRR Austin training panel (online preparation only).

Dependencies: pip install eccodes numcodecs numpy. No AWS account or credentials.
Only one 150 x 150 Zarr chunk per field and one indexed irradiance GRIB message
per forecast or analysis product are read, never full GRIB files.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.prepare_data import GRIB, ZARR

PUBLIC = ROOT / "data" / "austin_public.json"
DEFAULT_OUTPUT = ROOT / "data" / "austin_renewable_ml_training.json"
UTC = timezone.utc
START, STOP = date(2018, 8, 9), date(2018, 8, 24)
CYCLE_HOUR = 12
LEADS = (2, 8, 13)
CHUNK_SIDE = 150
METADATA_LIMIT = 70_000
FIELD_LIMIT = 3_000_000
FIELDS = {
    "temperature_kelvin": ("2m_above_ground", "TMP"),
    "hub_u_ms": ("80m_above_ground", "UGRD"),
    "hub_v_ms": ("80m_above_ground", "VGRD"),
}
GRIB_LOCK = Lock()


def get_bytes(url, limit, *, byte_range=None):
    headers = {"User-Agent": "GRIDMIND bounded public HRRR preparation"}
    if byte_range is not None:
        first, last = byte_range
        headers["Range"] = f"bytes={first}-{last}"
    request = Request(url, headers=headers)
    for attempt in range(2):
        try:
            with urlopen(request, timeout=15) as response:
                if byte_range is not None and (
                    response.status != 206
                    or not response.headers.get("Content-Range", "").startswith(f"bytes {first}-{last}/")
                ):
                    raise ValueError(f"Archive did not honor the bounded range: {url}")
                payload = response.read(limit + 1)
                if len(payload) > limit or (byte_range is not None and len(payload) != last - first + 1):
                    raise ValueError(f"Unexpected archive byte count: {url}")
                return payload
        except HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt:
                raise
        except (TimeoutError, URLError):
            if attempt:
                raise
    raise RuntimeError("Unreachable retry state")


def zarr_point(issue, product, field, gridpoint):
    import numpy as np
    from numcodecs.blosc import decompress

    level, key = FIELDS[field]
    chunk_x, x = divmod(gridpoint["x_index"], CHUNK_SIDE)
    chunk_y, y = divmod(gridpoint["y_index"], CHUNK_SIDE)
    suffix = f"{chunk_y}.{chunk_x}" if product == "anl" else f"0.{chunk_y}.{chunk_x}"
    url = (f"{ZARR}/sfc/{issue:%Y%m%d}/{issue:%Y%m%d_%H}z_{product}.zarr"
           f"/{level}/{key}/{level}/{key}/{suffix}")
    compressed = get_bytes(url, FIELD_LIMIT)
    decoded = np.frombuffer(decompress(compressed), dtype="<f2").reshape(-1, CHUNK_SIDE, CHUNK_SIDE)
    values = [float(value) for value in decoded[:, y, x]]
    return values, {
        "url": url, "sha256": sha256(compressed).hexdigest(), "bytes": len(compressed),
    }


def irradiance_point(issue, lead, gridpoint):
    from eccodes import codes_grib_find_nearest, codes_new_from_message, codes_release

    url = f"{GRIB}/hrrr.{issue:%Y%m%d}/conus/hrrr.t{issue:%H}z.wrfsfcf{lead:02d}.grib2"
    index = get_bytes(url + ".idx", METADATA_LIMIT)
    fields = index.decode().splitlines()
    for i, line in enumerate(fields[:-1]):
        if ":DSWRF:surface:" not in line:
            continue
        start, stop = (int(fields[j].split(":")[1]) for j in (i, i + 1))
        if not 0 < stop - start <= FIELD_LIMIT:
            raise ValueError(f"Irradiance message exceeds byte limit: {url}")
        # The query provides a separate cache key from any full-object request.
        payload = get_bytes(url + "?range=dswrf", stop - start, byte_range=(start, stop - 1))
        with GRIB_LOCK:
            handle = codes_new_from_message(payload)
            try:
                result = codes_grib_find_nearest(handle, gridpoint["latitude"], gridpoint["longitude"])[0]
                value = float(result["value"])
            finally:
                codes_release(handle)
        return value, {
            "url": url, "index_url": url + ".idx", "index_sha256": sha256(index).hexdigest(),
            "byte_range": [start, stop - 1], "sha256": sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    raise ValueError(f"No DSWRF field in {url}.idx")


def hour_string(stamp):
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def key_text(task):
    kind, stamp, product, field = task
    return f"{kind}:{hour_string(stamp)}:{product}:{field}"


def training_rows(gridpoint):
    issues = [datetime.combine(START + timedelta(days=i), time(CYCLE_HOUR), UTC)
              for i in range((STOP - START).days)]
    predictions = [(issue, issue + timedelta(hours=lead), lead) for issue in issues for lead in LEADS]
    groups = {}
    for issue, target, lead in predictions:
        groups[issue, target, lead] = {
            "forecast": {**{field: ("zarr", issue, "fcst", field) for field in FIELDS},
                         "irradiance_wm2": ("grib", issue, lead, "DSWRF")},
            "analysis": {**{field: ("zarr", target, "anl", field) for field in FIELDS},
                         "irradiance_wm2": ("grib", target, 0, "DSWRF")},
        }

    def fetch(task):
        kind, stamp, product, field = task
        return (zarr_point(stamp, product, field, gridpoint) if kind == "zarr"
                else irradiance_point(stamp, product, gridpoint))

    tasks = sorted({key for group in groups.values() for side in group.values() for key in side.values()})
    data, audit, errors = {}, {}, {}
    with ThreadPoolExecutor(max_workers=20) as workers:
        pending = {workers.submit(fetch, task): task for task in tasks}
        for i, job in enumerate(as_completed(pending), start=1):
            task = pending[job]
            try:
                data[task], audit[key_text(task)] = job.result()
            except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError) as error:
                errors[key_text(task)] = str(error)
            if i % 30 == 0 or i == len(tasks):
                print(f"Resolved {i}/{len(tasks)} fixed products; {len(errors)} unavailable", flush=True)
    rows = []
    for (issue, target, lead), group in groups.items():
        item = {"forecast_issued_at": hour_string(issue), "forecast_valid_at": hour_string(target)}
        for name, keys in group.items():
            item[name] = {}
            for field, task in keys.items():
                known = data.get(task)
                value = known[lead - 1 if name == "forecast" else 0] if isinstance(known, list) else known
                # Record genuine missingness rather than substituting modeled fields.
                value = value if value is not None and math.isfinite(value) and value != -10000.0 else None
                item[name][field] = value
        rows.append(item)
    return rows, dict(sorted(audit.items())), dict(sorted(errors.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    options = parser.parse_args()
    raw_pack = PUBLIC.read_bytes().replace(b"\r\n", b"\n")
    gridpoint = json.loads(raw_pack)["manifest"]["weather_gridpoint"]
    rows, products, unavailable = training_rows(gridpoint)
    result = {
        "schema_version": 1,
        "source_pack_sha256": sha256(raw_pack).hexdigest(),
        "gridpoint": gridpoint,
        "source": "NOAA HRRR CONUS surface forecast and target-time analyses, public GRIB2 and Utah HRRR Zarr",
        "forecast_cycle_utc": CYCLE_HOUR,
        "lead_hours": list(LEADS),
        "issue_days_utc": [START.isoformat(), (STOP - timedelta(days=1)).isoformat()],
        "rows": rows,
        "archive_products": products,
        "unavailable_products": unavailable,
    }
    if options.output.resolve() == PUBLIC.resolve():
        parser.error("cannot overwrite the original Austin replay")
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {len(rows)} scheduled pairs and {len(products)} available public products to {options.output}", flush=True)


if __name__ == "__main__":
    main()
