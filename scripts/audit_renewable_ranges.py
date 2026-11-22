"""Audit Austin interval evidence; no downloads, fitting, or input mutations.

Optional `--meter` reads an existing local aggregate meter overlay. Only aggregate
evaluation metrics are printed. Requires pandas and NumPy for the offline audit
but neither is an application runtime dependency.
"""
from argparse import ArgumentParser
from pathlib import Path
import json

import numpy as np
import pandas as pd


def rows_with_utc(rows, stamp):
    frame = pd.DataFrame(rows)
    frame[stamp] = pd.to_datetime(frame[stamp], utc=True, errors="raise")
    if frame[stamp].duplicated().any():
        raise ValueError(f"Duplicate {stamp} rows")
    return frame.sort_values(stamp)


def unique_weather(frame, issued, valid, solar, wind):
    keys = [issued, valid]
    fields = [solar, wind]
    if frame.groupby(keys)[fields].nunique(dropna=False).gt(1).any().any():
        raise ValueError("Inconsistent repeated forecast for one issue/target")
    return (frame[keys + fields].drop_duplicates()
            .rename(columns=dict(zip(keys + fields, ["issue", "target", "solar", "wind"]))))


def audit(public, overlay=None):
    weather = rows_with_utc(public["ticks"], "timestamp")
    clocks = ["forecast_issued_at", "forecast_valid_at", "next_forecast_issued_at", "next_forecast_valid_at"]
    for name in clocks:
        weather[name] = pd.to_datetime(weather[name], utc=True, errors="raise")
    lag = pd.Timedelta(minutes=public["manifest"]["weather_publication_delay_minutes"])
    if (weather["next_forecast_issued_at"] + lag > weather["timestamp"]).any():
        raise ValueError("Look-ahead detected in outlook issue time")
    current = unique_weather(weather, *clocks[:2], "solar_factor", "wind_factor")
    outlook = unique_weather(weather, *clocks[2:], "forecast_solar_factor", "forecast_wind_factor")
    overlap = outlook.merge(current, on="target", suffixes=("_ahead", "_current"), validate="one_to_one")
    same_issue = overlap.issue_ahead.eq(overlap.issue_current)
    if not (
        overlap.loc[same_issue, "solar_ahead"].eq(overlap.loc[same_issue, "solar_current"]).all()
        and overlap.loc[same_issue, "wind_ahead"].eq(overlap.loc[same_issue, "wind_current"]).all()
    ):
        raise ValueError("Same issue and target have conflicting public values")
    summary = {
        "public_replay_rows": len(weather), "unique_outlook_issue_target_pairs": len(outlook),
        "target_overlap_with_current_model": len(overlap),
        "same_issue_model_repeats": int(same_issue.sum()),
        "different_issue_model_revisions_not_observations": int((~same_issue).sum()),
        "pre_replay_modeled_solar_hours": len(public["calibration_solar_factors"]),
        "measured_output_in_public_pack": {"solar": False, "wind": False},
    }
    if overlay is not None:
        summary["meter_evaluation"] = audit_meter(public, weather, overlay)
    return summary


def audit_meter(public, weather, overlay):
    meter = rows_with_utc([{"timestamp": stamp, **row} for stamp, row in overlay["ticks"].items()], "timestamp")
    train_end = pd.Timestamp(public["manifest"]["calibration_end_exclusive"])
    if meter.timestamp.min() < train_end:
        raise ValueError("Evaluation rows overlap the archived calibration window")
    # Recover the fixed training scale encoded in unsaturated rows. Each valid
    # rounded pair defines a feasible interval, so intersect intervals instead
    # of refitting on test observations. Never score clipped measured factors.
    solar_kw = meter["solar_kw"].astype(float)
    factors = meter["solar_factor"].astype(float)
    unsaturated = factors.between(.01, .99) & solar_kw.gt(0)
    if not unsaturated.any():
        raise ValueError("Cannot recover the frozen training scale from the rounded overlay")
    raw, factor = solar_kw[unsaturated], factors[unsaturated]
    scale_low = ((raw - .00005) / (factor + .000005)).max()
    scale_high = ((raw + .00005) / (factor - .000005)).min()
    if not 0 < scale_low <= scale_high:
        raise ValueError("Overlay has no consistent pre-trained scale; evaluate from raw input")
    bands = weather[["timestamp", "next_forecast_issued_at", "next_forecast_valid_at"]].merge(
        meter[["timestamp", "forecast_solar_range", "forecast_solar_factor"]], on="timestamp", validate="one_to_one"
    )
    bands[["low", "high"]] = bands["forecast_solar_range"].tolist()
    keys = ["next_forecast_issued_at", "next_forecast_valid_at"]
    if bands.groupby(keys)[["low", "high", "forecast_solar_factor"]].nunique().gt(1).any().any():
        raise ValueError("One issued forecast has inconsistent range values")
    if not ((0 <= bands.low) & (bands.low <= bands.high) & (bands.high <= 1)).all():
        raise ValueError("Invalid normalized solar bounds")
    meter["target"] = meter.timestamp.dt.floor("h")
    agg = meter.groupby("target").agg(solar_kw=("solar_kw", "mean"), samples=("solar_kw", "count"))
    aligned = bands[keys + ["low", "high", "forecast_solar_factor"]].drop_duplicates().merge(
        agg[agg.samples.eq(4)], left_on="next_forecast_valid_at", right_index=True, validate="one_to_one"
    )
    # The uncertainties from the overlay's decimal rounding are tracked rather
    # than scoring a boundary contact as an exact result.
    def scores(sample):
        all_inside = ((sample.solar_kw - .00005 >= sample.low * scale_high)
                      & (sample.solar_kw + .00005 <= sample.high * scale_low))
        all_outside = ((sample.solar_kw + .00005 < sample.low * scale_low)
                       | (sample.solar_kw - .00005 > sample.high * scale_high))
        # A recorded zero with an exact zero-only modeled interval is a hit at
        # the precision of this dataset.
        zero_only = (sample.solar_kw.eq(0) & sample.low.eq(0) & sample.high.eq(0))
        hits = all_inside | zero_only
        return {
            "targets": len(sample), "hits": int(hits.sum()), "misses": int(all_outside.sum()),
            "rounding_indeterminate": int((~hits & ~all_outside).sum()),
            "mean_width_kw": round(float(np.mean(sample.high - sample.low) * (scale_low + scale_high) / 2), 4)
                if len(sample) else None,
        }
    return {
        "earlier_fit_pairs_reported": overlay["manifest"]["calibration_pairs"],
        "holdout_complete_hours": scores(aligned),
        "nonzero_forecast_hours": scores(aligned[aligned.forecast_solar_factor.gt(0)]),
        "fitting_or_tuning_done": False, "measured_wind_available": False,
    }


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--public", type=Path, default=Path("data/austin_public.json"))
    parser.add_argument("--meter", type=Path)
    args = parser.parse_args()
    public = json.loads(args.public.read_text(encoding="utf-8"))
    overlay = json.loads(args.meter.read_text(encoding="utf-8")) if args.meter else None
    print(json.dumps(audit(public, overlay), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
