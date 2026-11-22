import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("numpy")

from scripts.audit_renewable_ranges import audit


@pytest.fixture
def public():
    return json.loads((Path(__file__).resolve().parents[1] / "data/austin_public.json").read_text())


def test_public_replay_does_not_duplicate_evidence(public):
    summary = audit(public)
    assert summary["public_replay_rows"] == 120
    assert summary["unique_outlook_issue_target_pairs"] == 30
    assert summary["same_issue_model_repeats"] == 27
    assert summary["different_issue_model_revisions_not_observations"] == 2
    assert summary["measured_output_in_public_pack"] == {"solar": False, "wind": False}


def test_audit_excludes_clipping_bias_and_duplicate_intervals(public):
    overlay = {"manifest": {"calibration_pairs": 11}, "ticks": {}}
    for row in public["ticks"]:
        stamp = row["timestamp"]
        over_capacity = stamp.startswith("2018-08-24T20:")
        overlay["ticks"][stamp] = {
            "solar_kw": 11.0 if over_capacity else 3.0,
            "solar_factor": 1.0 if over_capacity else .3,
            "forecast_solar_factor": .3,
            "forecast_solar_range": [.2, 1.0],
        }
    results = audit(public, overlay)["meter_evaluation"]["holdout_complete_hours"]
    assert results == {"targets": 29, "hits": 28, "misses": 1,
                       "rounding_indeterminate": 0, "mean_width_kw": 8.0}


def test_rejects_a_forecast_that_was_not_available_at_replay_time(public):
    case = copy.deepcopy(public)
    case["ticks"][0]["next_forecast_issued_at"] = case["ticks"][0]["timestamp"]
    with pytest.raises(ValueError, match="Look-ahead"):
        audit(case)
