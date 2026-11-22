from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gridmind.datasets import wind_yield
from scripts.prepare_outlook_sensitivity import (
    SOURCE, inferred_hub_speed, prepare, solar_range, wind_range,
)


class OutlookSensitivityTest(unittest.TestCase):
    def test_solar_scenario_is_relative_and_respects_nameplate_and_true_zero(self):
        self.assertIsNone(solar_range(None, 0.20))
        self.assertEqual(solar_range(0.0, 0.20), (0.0, 0.0))
        self.assertEqual(solar_range(0.5, 0.20), (0.4, 0.6))
        self.assertEqual(solar_range(1.0, 0.20), (0.8, 1.0))

    def test_wind_inversion_and_cut_out(self):
        for point in (None, 0.0, 1.0):
            self.assertIsNone(inferred_hub_speed(point))
            self.assertIsNone(wind_range(point, None, 1.0))
        point = 0.03819
        speed = inferred_hub_speed(point)
        self.assertAlmostEqual(wind_yield(speed), point)
        self.assertAlmostEqual(wind_range(point, speed, 1.0)[0], 0.0096303843, places=9)
        self.assertAlmostEqual(wind_range(point, speed, 1.0)[1], 0.0826710356, places=9)
        # Both endpoints are zero, but a sufficiently wide perturbation includes rated power.
        self.assertEqual(wind_range(point, speed, 25.0), (0.0, 1.0))

    def test_bundled_replay_provenance_and_demo_ranges(self):
        original = SOURCE.read_bytes()
        report = prepare(SOURCE)
        self.assertEqual(SOURCE.read_bytes(), original)
        self.assertEqual(prepare(SOURCE), report)
        self.assertEqual(report["source"]["sha256"], sha256(original).hexdigest())
        summary = report["summary"]
        self.assertEqual((summary["replay_slots"], summary["unique_target_forecasts"]), (120, 30))
        self.assertEqual((summary["solar_scenario_count"], summary["wind_scenario_count"]), (30, 30))
        vintage = summary["same_target_vintage_check"]
        self.assertEqual((vintage["same_cycle"], vintage["different_cycle"], vintage["no_match"]), (27, 2, 1))
        self.assertEqual([row["wind"]["revision"] for row in vintage["comparisons"]], [0.10339, -0.00457])
        self.assertEqual(
            [row["wind"]["revision_value_within_scenario"] for row in vintage["comparisons"]], [False, True]
        )
        for row in report["outlooks"]:
            self.assertEqual(row["slot_count"], 4)
            for kind in ("solar", "wind"):
                low, high = row[f"{kind}_scenario_factor_range"]
                self.assertTrue(0 <= low <= row[f"forecast_{kind}_factor"] <= high <= 1)
        demo = next(row for row in report["outlooks"] if row["forecast_valid_at"] == "2018-08-24T21:00:00+00:00")
        self.assertEqual(demo["forecast_solar_factor"], 0.67876)
        self.assertEqual(demo["forecast_wind_factor"], 0.03819)
        self.assertEqual([round(46 * factor, 2) for factor in demo["solar_scenario_factor_range"]], [24.98, 37.47])
        self.assertEqual([round(6 * factor, 2) for factor in demo["solar_scenario_factor_range"]], [3.26, 4.89])
        self.assertEqual([round(18 * factor, 2) for factor in demo["wind_scenario_factor_range"]], [0.17, 1.49])

    def test_zero_sensitivity_exactly_preserves_archived_point_estimates(self):
        rows = prepare(SOURCE, pv_delta=0, wind_delta_ms=0)["outlooks"]
        for row in rows:
            for kind in ("solar", "wind"):
                self.assertEqual(row[f"{kind}_scenario_factor_range"], [row[f"forecast_{kind}_factor"]] * 2)

    def test_rejects_forecast_not_yet_available_and_conflicting_duplicate(self):
        with TemporaryDirectory() as folder:
            destination = Path(folder) / "fixture.json"
            pack = json.loads(SOURCE.read_text(encoding="utf-8"))
            pack["ticks"][0]["next_forecast_issued_at"] = "2018-08-24T17:15:00Z"
            destination.write_text(json.dumps(pack), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not available as of replay slot"):
                prepare(destination)

            pack = json.loads(SOURCE.read_text(encoding="utf-8"))
            pack["ticks"][1]["forecast_solar_factor"] += 0.01
            destination.write_text(json.dumps(pack), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Conflicting outlook factors"):
                prepare(destination)
