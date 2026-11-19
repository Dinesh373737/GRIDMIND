import unittest

from gridmind.engine import tick


def frame(step=0, **fields):
    return {
        "step": step,
        "timestamp": "2018-08-24T21:00:00+00:00",
        "solar_factor": 1,
        "demand_error_factors": [0.85, 1.0, 1.15],
        **fields,
    }


def participant(identifier, load=0, essential=0, solar=0, **fields):
    return {"id": identifier, "name": identifier, "load_kw": load, "essential_kw": essential,
            "solar_kw": solar, **fields}


def order(identifier, participant_id, side, quantity, price=10, urgency=0):
    return {"id": identifier, "participant_id": participant_id, "side": side,
            "quantity_kwh": quantity, "price": price, "urgency": urgency}


class EngineTest(unittest.TestCase):
    def test_manual_requests_respect_essential_needs_and_supply(self):
        participants = [participant("Hospital", load=36, essential=36),
                        participant("School", load=30), participant("Seller", solar=48)]
        orders = [order("seller", "Seller", "sell", 20, price=6),
                  order("school", "School", "buy", 7.5, urgency="urgent"),
                  order("hospital", "Hospital", "buy", 9, urgency="routine")]
        result = tick({"scenario": "historical"}, frame(outage=True), participants, orders)
        fills = result["market"]["fills"]
        self.assertEqual([(f["buyer_id"], f["quantity_kwh"], f["priority"]) for f in fills],
                         [("Hospital", 9, "essential"), ("School", 3, "standard")])
        self.assertEqual(result["totals"]["grid_import_kwh"], 0)
        self.assertEqual(result["totals"]["traded_kwh"], 12)
        self.assertEqual(result["totals"]["unserved_kwh"], 4.5)

    def test_essential_tranches_are_cleared_before_other_demand(self):
        participants = [participant("Hospital", load=24, essential=4, urgency=100),
                        participant("Clinic", load=4, essential=4), participant("Seller", solar=12)]
        result = tick({}, frame(outage=True), participants)
        fills = result["market"]["fills"]
        self.assertEqual([(f["buyer_id"], f["quantity_kwh"], f["priority"]) for f in fills],
                         [("Hospital", 1, "essential"), ("Clinic", 1, "essential"), ("Hospital", 1, "standard")])
        self.assertEqual(len({f["id"] for f in fills}), 3)

    def test_historical_missing_observation_cannot_imply_recovery(self):
        participants = [participant("Buyer", load=12), participant("Seller", solar=12)]
        first = tick({}, frame(outage=True), participants)
        gap = tick({}, frame(1, outage=None), participants, prior=first["state"])
        restored = tick({}, frame(2, outage=False), participants, prior=gap["state"])
        self.assertTrue(gap["market"]["open"])
        self.assertEqual(restored["grid"]["transition"], "restored")
        self.assertEqual(restored["market"]["fills"], [])
        self.assertEqual(restored["totals"]["served_kwh"], 3)

    def test_load_added_to_locality_requires_sustained_pressure(self):
        participants = [participant("Base", load=8), participant("New", load=10, active_from_step=1)]
        config = {"scenario": "stress", "import_limit_kw": 10, "trip_ticks": 2, "recovery_ticks": 2}
        first = tick(config, frame(), participants)
        warning = tick(config, frame(1), participants, prior=first["state"])
        failure = tick(config, frame(2), participants, prior=warning["state"])
        recovering = tick(config, frame(3, load_factor=0.5), participants, prior=failure["state"])
        restored = tick(config, frame(4, load_factor=0.5), participants, prior=recovering["state"])
        self.assertEqual(first["grid"]["risk_next_hour"], 0)
        self.assertEqual(warning["grid"]["risk_next_hour"], 1)
        self.assertEqual(warning["grid"]["status"], "connected")
        self.assertEqual(failure["grid"]["transition"], "islanded")
        self.assertEqual(recovering["grid"]["status"], "islanded")
        self.assertEqual(restored["grid"]["transition"], "restored")

    def test_unavailable_island_cannot_trade(self):
        participants = [participant("Need", load=12),
                        participant("BehindGridInverter", solar=99, island_capable=False)]
        result = tick({}, frame(outage=True), participants)
        self.assertTrue(result["market"]["open"])
        self.assertFalse(result["grid"]["island_energized"])
        self.assertEqual(result["market"]["fills"], [])
        self.assertEqual(result["totals"]["unserved_kwh"], 3)

    def test_battery_cannot_sell_past_its_power_or_reserve(self):
        participants = [participant("Need", load=50),
                        participant("Storage", battery_kwh=8, battery_kw=4, initial_soc=0.25)]
        first = tick({}, frame(outage=True), participants)
        second = tick({}, frame(1, outage=True), participants, prior=first["state"])
        self.assertEqual(first["totals"]["traded_kwh"], 0.752)
        self.assertAlmostEqual(first["state"]["storage_kwh"]["Storage"], 1.2)
        self.assertEqual(second["totals"]["traded_kwh"], 0)

    def test_forecasts_are_advisory_and_reference_has_no_trading(self):
        participants = [participant("Need", load=16), participant("Seller", solar=16)]
        config = {"scenario": "scheduled", "outage_step": 0, "outage_duration": 4}
        clear = tick(config, frame(forecast_solar_factor=0.85), participants)
        cloudy = tick(config, frame(forecast_solar_factor=0.10), participants)
        reference = tick({**config, "scenario": "reference"}, frame(forecast_solar_factor=0.10), participants)
        self.assertNotEqual(clear["renewables"], cloudy["renewables"])
        self.assertEqual(clear["market"], cloudy["market"])
        self.assertEqual(reference["grid"]["status"], "islanded")
        self.assertEqual(reference["totals"]["traded_kwh"], 0)
        self.assertGreater(reference["totals"]["unserved_kwh"], cloudy["totals"]["unserved_kwh"])

    def test_retry_is_deterministic_and_ids_are_run_scoped(self):
        participants = [participant("Need", load=12), participant("Seller", solar=12)]
        current = frame(outage=True)
        first = tick({"run_id": "one"}, current, participants)
        self.assertEqual(first, tick({"run_id": "one"}, current, reversed(participants)))
        other = tick({"run_id": "two"}, current, participants)
        self.assertNotEqual(first["market"]["fills"][0]["id"], other["market"]["fills"][0]["id"])

    def test_stress_augmentation_is_seeded_and_preserves_source_measures(self):
        participants = [participant("Need", load=12)]
        current = frame(measurements={"regional_demand_mw": 71000}, demand_error_factors=[0.8, 1.2])
        config = {"scenario": "stress", "seed": 42}
        result = tick(config, current, participants)
        same_hour = tick(config, {**current, "step": 1}, participants)
        reference = tick({**config, "scenario": "reference", "reference_scenario": "stress"}, current, participants)
        self.assertEqual(result["source"]["measurements"], current["measurements"])
        self.assertEqual(result["source"]["augmentation"], same_hour["source"]["augmentation"])
        self.assertEqual(result["totals"]["demand_kwh"], reference["totals"]["demand_kwh"])
        self.assertNotEqual(result["totals"]["demand_kwh"], 3)


if __name__ == "__main__":
    unittest.main()
