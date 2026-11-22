from hashlib import sha256
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from gridmind import datasets, worker
from gridmind.api import create_app
from gridmind.db import ReplayDataset, Run, connect, initialize
from gridmind.engine import tick
from gridmind.replay import import_packs, read_ticks
from gridmind.supervisor import Supervisor


TARGET_LABEL = "NOAA HRRR weather-model analysis, not measured generation"
PRODUCER = {"id": "hybrid", "solar_kw": 40, "wind_kw": 20, "load_kw": 1}
CONFIG = {"scenario": "historical", "interval_minutes": 15}


@pytest.fixture
def local_packs(monkeypatch, tmp_path):
    raw = (Path(__file__).resolve().parents[1] / "data" / "austin_public.json").read_bytes()
    source = tmp_path / "austin_public.json"
    source.write_bytes(raw)
    artifact = tmp_path / "austin_renewable_ml.json"
    monkeypatch.setattr(datasets, "DATA", tmp_path)
    monkeypatch.setattr(datasets, "LOCAL_DATA", tmp_path)
    monkeypatch.setenv("GRIDMIND_EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("GRIDMIND_MODE", "local")
    monkeypatch.delenv("GRIDMIND_API_KEY", raising=False)
    first = json.loads(raw)["ticks"][0]
    report = {
        "schema_version": 1,
        "source_pack_sha256": sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
        "method": {"id": "fixture", "target_label": TARGET_LABEL},
        "outlooks": [{
            "forecast_issued_at": first["next_forecast_issued_at"].replace("Z", "+00:00"),
            "forecast_valid_at": first["next_forecast_valid_at"].replace("Z", "+00:00"),
            "solar_scenario_factor_range": [0.7, 0.95],
            "wind_scenario_factor_range": [0.04, 0.12],
            "solar_margin_factor": 0.2,
            "wind_margin_factor": 0.07,
        }],
    }
    return source, artifact, report


def write_report(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_exact_issue_valid_join_and_capacity_scaling(local_packs):
    _, artifact, report = local_packs
    # Same target as the fifth replay tick, but a different forecast cycle.
    report["outlooks"].append({**report["outlooks"][0], "forecast_issued_at": "2018-08-24T00:00:00Z",
                               "forecast_valid_at": "2018-08-24T20:00:00Z"})
    legacy_frame = datasets.engine_frame(datasets.load_replay(), 0)
    write_report(artifact, report)
    ticks = datasets.load_replay()
    assert ticks[0].renewable_outlook == ticks[3].renewable_outlook
    assert ticks[4].renewable_outlook is None
    assert datasets.replay_manifest()["renewable_outlook"]["artifact_sha256"] == sha256(artifact.read_bytes()).hexdigest()

    result = tick(CONFIG, datasets.engine_frame(ticks, 0), [PRODUCER])
    outlook = result.pop("renewables")[0]
    assert outlook["range_kind"] == "scenario"
    assert outlook["expected_generation_kw"] == pytest.approx(40 * 0.82621 + 20 * 0.0774)
    assert outlook["range_low_kw"] == pytest.approx(40 * 0.7 + 20 * 0.04)
    assert outlook["range_high_kw"] == pytest.approx(40 * 0.95 + 20 * 0.12)
    assert outlook["model_error_margin_kw"] == pytest.approx(40 * 0.2 + 20 * 0.07)
    assert outlook["model_error_target"] == TARGET_LABEL
    baseline = tick(CONFIG, legacy_frame, [PRODUCER])
    baseline.pop("renewables")
    assert result == baseline  # Dispatch, grid risk, orders, traces and settlement are unchanged.


def test_existing_crlf_checkout_uses_canonical_hashes(local_packs):
    source, artifact, report = local_packs
    source.write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    canonical_artifact = (json.dumps(report, indent=2) + "\n").encode()
    artifact.write_bytes(canonical_artifact.replace(b"\n", b"\r\n"))
    assert datasets.load_replay()[0].renewable_outlook is not None
    manifest = datasets.replay_manifest()["renewable_outlook"]
    assert manifest["artifact_sha256"] == sha256(canonical_artifact).hexdigest()
    assert manifest["source_pack_sha256"] == report["source_pack_sha256"]


def test_missing_factors_are_not_zero_and_zero_is_a_valid_margin(local_packs):
    _, artifact, report = local_packs
    report["outlooks"][0].update(solar_margin_factor=0, wind_margin_factor=None,
                                 solar_scenario_factor_range=None)
    write_report(artifact, report)
    frame = datasets.engine_frame(datasets.load_replay(), 0)
    solar = {"id": "solar", "solar_kw": 40}
    wind = {"id": "wind", "wind_kw": 20}
    late = {"id": "late", "solar_kw": 10, "active_from_step": 1}
    advisory = {r["participant_id"]: r for r in tick(CONFIG, frame, [PRODUCER, solar, wind, late])["renewables"]}
    assert "late" not in advisory
    assert advisory["solar"]["range_low_kw"] is None
    assert "range_kind" not in advisory["solar"]
    assert advisory["solar"]["model_error_margin_kw"] == 0
    assert advisory["solar"]["model_error_target"] == TARGET_LABEL
    assert advisory["wind"]["range_kind"] == "scenario"
    assert "model_error_margin_kw" not in advisory["wind"]
    assert advisory["hybrid"]["range_high_kw"] is None
    assert "model_error_margin_kw" not in advisory["hybrid"]


def test_meter_data_stays_separate_and_outlook_synthetic_regime_drops_weather_model_claims(local_packs):
    source, artifact, report = local_packs
    write_report(artifact, report)
    first = json.loads(source.read_bytes())["ticks"][0]
    write_report(artifact.with_name("austin_meter_overlay.json"), {
        "manifest": {"source": "test meter"}, "demand_error_factors": [1.0],
        "ticks": {first["timestamp"]: {"forecast_solar_factor": 0.4, "forecast_solar_range": [0.3, 0.6],
                                        "data_kind": "meter_profile"}},
    })
    ticks = datasets.load_replay()
    solar = {"id": "solar", "solar_kw": 40}
    meter = datasets.engine_frame(ticks, 0)
    meter_output = tick(CONFIG, meter, [solar])["renewables"][0]
    assert meter_output["range_low_kw"] == 12 and meter_output["range_high_kw"] == 24
    assert "range_kind" not in meter_output and "model_error_margin_kw" not in meter_output
    assert "renewable_outlook" not in meter

    archived = datasets.engine_frame(ticks, 1)
    # Shift the scenario phase independently of the archived replay offset, as the real worker does.
    synthetic = datasets.augment_weather({**archived, "step": 6}, 42, 15)
    assert synthetic["weather"]["outlook_source_kind"] == "synthetic"
    synthetic_output = tick(CONFIG, synthetic, [PRODUCER])["renewables"][0]
    assert synthetic_output["range_low_kw"] is None and "model_error_margin_kw" not in synthetic_output
    assert "range_kind" not in synthetic_output
    # Keep the meter calculation's existing synthetic scaling rather than relabeling it.
    changed_meter = datasets.augment_weather({**meter, "step": 6}, 42, 15)
    assert 0 < tick(CONFIG, changed_meter, [solar])["renewables"][0]["range_high_kw"] < 24

    # The current observation being synthetic does not invalidate a still-archived outlook.
    archived_outlook = datasets.augment_weather({**archived, "step": 22}, 42, 15)
    assert archived_outlook["weather"]["source_kind"] == "synthetic"
    assert tick(CONFIG, archived_outlook, [PRODUCER])["renewables"][0]["range_kind"] == "scenario"


@pytest.mark.parametrize("mutation, error", [
    (lambda a: a.update(schema_version=True), "schema version"),
    (lambda a: a.update(source_pack_sha256="0" * 64), "source hash"),
    (lambda a: a["method"].pop("target_label"), "target label"),
    (lambda a: a["outlooks"][0].update(solar_margin_factor=float("nan")), "finite number"),
    (lambda a: a["outlooks"][0].update(wind_margin_factor=True), "finite number"),
    (lambda a: a["outlooks"][0].update(wind_margin_factor=1.001), "finite number"),
    (lambda a: a["outlooks"][0].update(solar_scenario_factor_range=[0.85, 0.9]), "enclose"),
    (lambda a: a["outlooks"][0].update(solar_scenario_factor_range=[0.95, 0.7]), "enclose"),
    (lambda a: a["outlooks"][0].update(forecast_valid_at="2018-08-24T19:00:00"), "ISO UTC"),
    (lambda a: a["outlooks"].append({**a["outlooks"][0], "forecast_valid_at": "2018-08-24T19:00:00Z"}), "unique"),
])
def test_rejects_invalid_artifact(local_packs, mutation, error):
    _, artifact, report = local_packs
    mutation(report)
    write_report(artifact, report)
    with pytest.raises(ValueError, match=error):
        datasets.load_replay()


def test_complete_meter_overlay_does_not_create_a_new_version(local_packs, tmp_path):
    source, artifact, report = local_packs
    write_report(artifact.with_name("austin_meter_overlay.json"), {
        "manifest": {"source": "test meter"}, "demand_error_factors": [1.0],
        "ticks": {
            row["timestamp"]: {"data_kind": "meter_profile", "forecast_solar_range": [0.0, 1.0]}
            for row in json.loads(source.read_bytes())["ticks"]
        },
    })
    engine, factory = connect(f"sqlite:///{tmp_path / 'test.sqlite'}")
    try:
        initialize(engine, factory)
        assert import_packs(factory) == ["austin"]
        write_report(artifact, report)
        assert import_packs(factory) == ["austin"]
        with factory() as db:
            assert "renewable_outlook" not in db.get(ReplayDataset, "austin").manifest
    finally:
        engine.dispose()


def test_new_runs_use_current_artifact_while_existing_runs_remain_pinned(local_packs, monkeypatch, tmp_path):
    source, artifact, report = local_packs
    monkeypatch.setattr(Supervisor, "start", lambda self, run_id: None)
    url = f"sqlite:///{tmp_path / 'test.sqlite'}"
    app = create_app(url)
    with TestClient(app) as api:
        boot = api.get("/api/bootstrap").json()
        assert [p["id"] for p in boot["packs"]] == ["austin"]
        locality = boot["localities"][0]
        request = {"locality_id": locality["id"], "steps": 1}
        original = api.post("/api/runs", json=request).json()
        assert original["replay_pack"] == "austin"
        old_id = original["id"]
        with app.state.factory.begin() as db:
            run = db.get(Run, old_id)
            old = db.get(ReplayDataset, "austin")
            old_digest = old.digest
            public = json.loads(source.read_bytes())
            manifest = {**public["manifest"], "interval_minutes": 15,
                        "demand_error_factors": public["demand_error_factors"]}
            rows = [{"data_kind": "regional_demand_proxy", "outlook_condition": None, "forecast_solar_range": None, **row}
                    for row in public["ticks"]]
            assert old_digest == sha256(json.dumps({"manifest": manifest, "rows": rows}, sort_keys=True,
                                                   allow_nan=False).encode()).hexdigest()
            result, pending = worker.calculate(db, run, read_ticks(db, run.dataset_id), tuple(public["demand_error_factors"]))
            worker.settle(db, run, result, pending)
            worker.finish(db, run, "completed")
        worker.deliver(app.state.factory, old_id)
        original_events = api.get(f"/api/runs/{old_id}/events").json()
        original_outlook = api.get(f"/api/runs/{old_id}/outlook").json()
        assert not any("range_kind" in row for row in original_outlook)

    write_report(artifact, report)
    app = create_app(url)
    with TestClient(app) as api:
        boot = api.get("/api/bootstrap").json()
        assert boot["localities"][0]["replay_pack"] == "austin"
        assert len(boot["packs"]) == 2
        assert boot["packs"][0]["id"] == "austin" and "Fixed version" not in boot["packs"][0]["name"]
        pinned = boot["packs"][1]
        assert pinned["id"].startswith("austin-") and "Fixed version" in pinned["name"]
        assert api.get(f"/api/runs/{old_id}/events").json() == original_events
        assert api.get(f"/api/runs/{old_id}/outlook").json() == original_outlook

        new = api.post("/api/runs", json=request).json()
        assert new["replay_pack"] == pinned["id"]
        # No runtime artifact access is needed for the worker/preview of a pinned run.
        artifact.unlink()
        preview = api.get(f'/api/runs/{new["id"]}/preview').json()
        coop = next(row for row in preview["renewables"] if "Solar and storage" in row["participant_name"])
        assert coop["range_kind"] == "scenario" and coop["model_error_margin_kw"] == pytest.approx(46 * 0.2)
        assert coop["model_error_target"] == TARGET_LABEL
        with app.state.factory() as db:
            assert db.get(ReplayDataset, "austin").digest == old_digest
            assert read_ticks(db, "austin")[0].renewable_outlook is None
            assert read_ticks(db, pinned["id"])[0].renewable_outlook["method_id"] == "fixture"
