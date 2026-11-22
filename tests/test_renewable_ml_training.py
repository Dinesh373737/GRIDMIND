from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.train_renewable_ml import (
    ARTIFACT, PACK, TRAINING, aligned_samples, calibration_addon, fit_ridge,
    generate, margin, power_factor, predicted_error,
)


class RenewableMLTrainingTest(unittest.TestCase):
    def test_weather_proxy_preserves_real_zero_and_distinguishes_missing_fields(self):
        self.assertEqual(power_factor("solar", {"irradiance_wm2": 0, "temperature_kelvin": 290}), 0)
        self.assertIsNone(power_factor("solar", {"irradiance_wm2": 0}))
        self.assertAlmostEqual(
            power_factor("solar", {"irradiance_wm2": 786, "temperature_kelvin": 310.25}),
            0.678764448,
        )
        self.assertEqual(power_factor("wind", {"hub_u_ms": 0, "hub_v_ms": 0}), 0)
        self.assertIsNone(power_factor("wind", {"hub_u_ms": 0}))
        self.assertAlmostEqual(
            power_factor("wind", {"hub_u_ms": 0.353271484375, "hub_v_ms": 4.5}),
            0.038194416,
        )

    def test_ridge_and_calibration_rank_without_reading_test_targets(self):
        day = datetime.fromisoformat("2018-08-10").date()
        fit = [(day, lead, p, p + 0.0625) for lead in (2, 8, 13) for p in (0.125, 0.25, 0.5)]
        coefficients = fit_ridge(fit)
        self.assertAlmostEqual(predicted_error(coefficients, 0.5, 13), 0.0625)
        calibration = [(day, 8, 0.125, 0.125 + e) for e in (0.03125, 0.0625, 0.125, 0.25)]
        addon, rank = calibration_addon(coefficients, calibration)
        self.assertEqual(rank, 4)
        self.assertAlmostEqual(margin(coefficients, addon, 0.5, 13), 0.25)
        with self.assertRaisesRegex(ValueError, "Too few calibration rows"):
            calibration_addon(coefficients, calibration[:3])

    def test_training_rejects_duplicate_pairs_replay_leak_and_grid_mismatch(self):
        training = json.loads(TRAINING.read_text(encoding="utf-8"))
        earliest = datetime.fromisoformat("2018-08-24T12:00:00Z")
        with self.assertRaisesRegex(ValueError, "must predate"):
            aligned_samples(training, datetime.fromisoformat("2018-08-24T00:00:00Z"))
        with self.assertRaisesRegex(ValueError, "Duplicate same-target"):
            aligned_samples({**training, "rows": training["rows"] + training["rows"][:1]}, earliest)
        with TemporaryDirectory() as work:
            wrong = Path(work) / "wrong.json"
            training["gridpoint"]["y_index"] = 668
            wrong.write_text(json.dumps(training), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "gridpoint"):
                generate(PACK, wrong)

    def test_checked_in_model_rebuild_and_expected_screenshot_margins(self):
        prepared = generate(PACK, TRAINING)
        self.assertEqual(prepared, json.loads(ARTIFACT.read_text(encoding="utf-8")))
        self.assertEqual(prepared["source_pack_sha256"], sha256(PACK.read_bytes()).hexdigest())
        self.assertEqual(prepared["training_data_sha256"], sha256(TRAINING.read_bytes()).hexdigest())
        self.assertFalse(prepared["method"]["observational_coverage_claim"])
        self.assertEqual(len(prepared["outlooks"]), 30)
        for row in prepared["outlooks"]:
            for tech in ("solar", "wind"):
                self.assertTrue(0 <= row[f"{tech}_margin_factor"] <= 1)
        sample = next(row for row in prepared["outlooks"] if row["forecast_valid_at"] == "2018-08-24T21:00:00+00:00")
        self.assertEqual(sample["solar_scenario_factor_range"], [0.543008, 0.814512])
        self.assertEqual(sample["wind_scenario_factor_range"], [0.0096303843, 0.0826710356])
        self.assertEqual(round(46 * sample["solar_margin_factor"], 2), 2.55)
        self.assertEqual(round(6 * sample["solar_margin_factor"], 2), 0.33)
        self.assertEqual(round(18 * sample["wind_margin_factor"], 2), 1.21)
        for tech, counts in {"solar": (19, 11, 8, 7), "wind": (21, 12, 9, 8)}.items():
            model = prepared["models"][tech]
            self.assertEqual((model["fit_n"], model["calibration_n"],
                              model["test"]["n"], model["test"]["analysis_hits"]), counts)
