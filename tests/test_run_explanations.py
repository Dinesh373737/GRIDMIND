import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from gridmind import api, explanations
from gridmind.db import (
    Locality, Order, Participant, ReplayDataset, Run, RunExplanation, RunParticipant, Step, Trade,
    connect, initialize, utcnow,
)

RUN_ID = "private-completed-run-id"
ACTIVE_ID = "private-active-run-id"
LOCALITY_ID = "private-locality-id"
TEXT = "The grid disconnected for one interval. The local market traded 2.5 kWh."


def result(status, explanation=None, *, cached=False):
    return {"status": status, "explanation": explanation, "cached": cached}


def model_output(text, reason="end_turn"):
    return {"stopReason": reason, "output": {"message": {"role": "assistant", "content": [{"text": text}]}}}


@pytest.fixture
def db_store(tmp_path):
    url = f"sqlite:///{tmp_path / 'test.sqlite'}"
    engine, factory = connect(url)
    initialize(engine, factory)
    with factory.begin() as db:
        db.add(ReplayDataset(id="private-pack-id", name="Private dataset", digest="f" * 64, manifest={}))
        db.flush()
        db.add(Locality(id=LOCALITY_ID, name="Private locality name", import_limit_kw=40,
                        island_capable=True, replay_pack="private-pack-id"))
        db.flush()
        for id_, status, completed in ((RUN_ID, "completed", 2), (ACTIVE_ID, "running", 1)):
            db.add(Run(id=id_, locality_id=LOCALITY_ID, dataset_id="private-pack-id", scenario="scheduled",
                       status=status, steps=2, completed_steps=completed, published_steps=completed, speed=1, seed=42,
                       snapshot={"locality_name": "Private locality name", "participants": ["Private participant"],
                                 "config": {"manual_note": "Private note", "interval_minutes": 15}}))
        for id_ in ("private-buyer-id", "private-seller-id"):
            db.add(Participant(id=id_, locality_id=LOCALITY_ID, facility_type_id="household",
                               name=id_, base_load_kw=10, essential_kw=5))
        db.flush()
        for id_ in ("private-buyer-id", "private-seller-id"):
            db.add(RunParticipant(run_id=RUN_ID, participant_id=id_))
        db.add_all([
            Step(run_id=RUN_ID, step=0, replay_at="2018-08-24T21:00:00Z", grid_status="connected",
                 served_kwh=15.75, unserved_kwh=0, essential_served_kwh=5.25,
                 essential_unserved_kwh=0, traded_kwh=0),
            Step(run_id=RUN_ID, step=1, replay_at="2018-08-24T21:15:00Z", grid_status="islanded",
                 served_kwh=9.25, unserved_kwh=3, essential_served_kwh=4.75,
                 essential_unserved_kwh=0.5, traded_kwh=2.5),
        ])
        db.flush()
        for side, party in (("buy", "buyer"), ("sell", "seller")):
            db.add(Order(id=f"private-{side}-order", run_id=RUN_ID, step=1, participant_id=f"private-{party}-id",
                         side=side, quantity_kwh=2.5, filled_kwh=2.5, price=12,
                         urgency="urgent", status="filled", manual=False))
        db.flush()
        db.add(Trade(id="private-trade-id", run_id=RUN_ID, step=1, buy_order_id="private-buy-order",
                     sell_order_id="private-sell-order", quantity_kwh=2.5, price=12))
    try:
        yield url, engine, factory
    finally:
        engine.dispose()


def test_cache_persists_and_only_aggregate_fields_reach_bedrock(db_store, monkeypatch):
    url, _, factory = db_store
    client = Mock()
    client.converse.return_value = model_output(f" {TEXT}\n")
    monkeypatch.setattr(explanations, "bedrock_client", lambda: client)

    assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 2) == result("ready", TEXT)
    params = client.converse.call_args.kwargs
    assert set(params) == {"modelId", "system", "messages", "inferenceConfig"}
    assert params["modelId"] == "amazon.nova-lite-v1:0"
    assert params["inferenceConfig"]["maxTokens"] == 220
    assert len(params["messages"]) == 1 and params["messages"][0]["role"] == "user"
    body = params["messages"][0]["content"][0]["text"]
    assert len(body) < 400
    assert json.loads(body) == {
        "scenario": "scheduled", "interval_count": 2, "islanded_intervals": 1,
        "served_kwh": 25, "unserved_kwh": 3, "essential_served_kwh": 10, "essential_unserved_kwh": 0.5,
        "traded_kwh": 2.5, "islanded_unserved_kwh": 3, "islanded_essential_unserved_kwh": 0.5,
        "trade_count": 1,
    }
    assert "private" not in json.dumps(params).lower()

    restarted_engine, restarted_factory = connect(url)
    try:
        assert explanations.explain_completed_run(restarted_factory, RUN_ID, "scheduled", 2) == result(
            "ready", TEXT, cached=True)
        client.converse.assert_called_once()
    finally:
        restarted_engine.dispose()


def test_concurrent_claims_only_invoke_bedrock_once(db_store, monkeypatch):
    _, _, factory = db_store
    before_insert = Barrier(2)
    invoke = Event()
    release_model = Event()
    mock_model = Mock()

    def synchronize_claims(session, *_):
        if any(isinstance(row, RunExplanation) for row in session.new):
            before_insert.wait(timeout=5)

    def model_call(_summary):
        mock_model()
        invoke.set()
        assert release_model.wait(timeout=5)
        return TEXT

    event.listen(factory, "before_flush", synchronize_claims)
    monkeypatch.setattr(explanations, "generate_explanation", model_call)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = {workers.submit(explanations.explain_completed_run, factory, RUN_ID, "scheduled", 2)
                       for _ in range(2)}
            try:
                assert invoke.wait(timeout=5)
                # Both initial reads saw no cache. The losing INSERT must resolve
                # while the winner still waits on Bedrock, not run a second call.
                done, running = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
                assert len(done) == len(running) == 1
                assert done.pop().result() == result("pending")
            finally:
                release_model.set()
            assert running.pop().result(timeout=5) == result("ready", TEXT)
    finally:
        event.remove(factory, "before_flush", synchronize_claims)
    mock_model.assert_called_once()


@pytest.mark.parametrize("failure", [
    TimeoutError("internal-provider-message"),
    model_output("", reason="content_filtered"),
    model_output("x" * (explanations.MAX_EXPLANATION_CHARS + 1)),
])
def test_bedrock_failure_is_unavailable_and_never_retried(db_store, monkeypatch, caplog, failure):
    _, _, factory = db_store
    client = Mock()
    if isinstance(failure, Exception):
        client.converse.side_effect = failure
    else:
        client.converse.return_value = failure
    monkeypatch.setattr(explanations, "bedrock_client", lambda: client)

    for _ in range(2):
        assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 2) == result("unavailable")
    client.converse.assert_called_once()
    assert "internal-provider-message" not in caplog.text
    with factory() as db:
        row = db.get(RunExplanation, RUN_ID)
        assert row.status == "unavailable" and row.explanation is None


def test_stale_claim_is_unavailable_and_never_recycled(db_store, monkeypatch):
    _, _, factory = db_store
    with factory.begin() as db:
        db.add(RunExplanation(run_id=RUN_ID, status="pending",
                              claimed_at=utcnow() - explanations.PENDING_WINDOW - timedelta(seconds=1)))
    model = Mock()
    monkeypatch.setattr(explanations, "generate_explanation", model)
    for _ in range(2):
        assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 2) == result("unavailable")
    model.assert_not_called()


def test_aws_client_uses_instance_role_fixed_region_and_no_sdk_retries(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    session = Mock()
    session.get_credentials.return_value = SimpleNamespace(method="env")
    monkeypatch.setattr(boto3.session, "Session", lambda: session)
    explanations.bedrock_client.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="EC2 instance role"):
            explanations.bedrock_client()
        session.client.assert_not_called()
        session.get_credentials.return_value = SimpleNamespace(method="iam-role")
        assert explanations.bedrock_client() is session.client.return_value
        args, kwargs = session.client.call_args
        assert args == ("bedrock-runtime",)
        assert set(kwargs) == {"region_name", "config"}
        assert kwargs["region_name"] == "us-east-1"
        config = kwargs["config"]
        assert (config.connect_timeout, config.read_timeout) == (2, 8)
        assert config.signature_version == "v4"
        assert config.ignore_configured_endpoint_urls
        assert config.retries["total_max_attempts"] == 1
    finally:
        explanations.bedrock_client.cache_clear()


def test_sdk_converse_signs_regional_request_with_role_and_never_retries(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    from botocore.awsrequest import AWSResponse
    from botocore.credentials import Credentials
    from botocore.exceptions import ClientError
    from botocore.session import Session
    from botocore.httpsession import URLLib3Session

    # SDK shapes, signing, endpoint selection and retry handling run normally.
    # The transport is stubbed before client creation; no requests can leave here.
    fake_error = {"message": "fixture model not ready"}
    requests = []

    def send(_session, request):
        requests.append(request)
        raw = SimpleNamespace(stream=lambda: [json.dumps(fake_error).encode()])
        return AWSResponse(request.url, 429, {"x-amzn-errortype": "ModelNotReadyException"}, raw)

    monkeypatch.setattr(URLLib3Session, "send", send)
    role = Session()
    role._credentials = Credentials("fixture-role-key", "fixture-role-secret", method="iam-role")
    role_session = boto3.session.Session(botocore_session=role)
    monkeypatch.setattr(boto3.session, "Session", lambda: role_session)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "fixture-bearer-not-to-use")
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "https://not-bedrock.example")
    explanations.bedrock_client.cache_clear()
    try:
        with pytest.raises(ClientError) as error:
            explanations.generate_explanation({"served_kwh": 25})
        assert error.value.response["Error"]["Code"] == "ModelNotReadyException"
        assert len(requests) == 1
        request = requests[0]
        assert request.url == "https://bedrock-runtime.us-east-1.amazonaws.com/model/amazon.nova-lite-v1%3A0/converse"
        assert request.headers["Authorization"].startswith(b"AWS4-HMAC-SHA256 Credential=fixture-role-key/")
    finally:
        explanations.bedrock_client.cache_clear()


@contextmanager
def app_for_tests(db_store):
    _, engine, factory = db_store
    app = api.create_app()
    # Route tests use an isolated real persistence layer without running the
    # production AWS/MySQL lifespan or touching external services.
    app.state.engine, app.state.factory = engine, factory
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()


def test_local_opt_in_does_not_enable_bedrock(db_store, monkeypatch):
    monkeypatch.setenv("GRIDMIND_BEDROCK_ENABLED", "1")
    monkeypatch.setenv("GRIDMIND_API_KEY", "test-key")
    monkeypatch.setattr(api, "cloud_config", lambda: None)
    invoke = Mock()
    monkeypatch.setattr(api, "explain_completed_run", invoke)
    with app_for_tests(db_store) as client:
        url = f"/api/runs/{RUN_ID}/explanation"
        assert client.post(url).status_code == 401
        assert client.post(url, headers={"X-API-Key": "test-key"}).json() == result("disabled")
    invoke.assert_not_called()


def test_aws_endpoint_auth_completed_only_and_report_fallback(db_store, monkeypatch):
    jwt = pytest.importorskip("jwt")
    monkeypatch.setattr(api, "cloud_config", lambda: SimpleNamespace(origins=("https://demo.example",)))

    def verify(_cloud, token):
        if token != "access-token":
            raise jwt.InvalidTokenError("invalid")

    monkeypatch.setattr(api, "verify_access_token", verify)
    model = Mock(side_effect=RuntimeError("provider-secret-details"))
    monkeypatch.setattr(explanations, "generate_explanation", model)
    auth = {"Authorization": "Bearer access-token"}
    url = f"/api/runs/{RUN_ID}/explanation"
    monkeypatch.delenv("GRIDMIND_BEDROCK_ENABLED", raising=False)
    with app_for_tests(db_store) as disabled:
        assert disabled.post(url, headers=auth).json() == result("disabled")
    model.assert_not_called()

    monkeypatch.setenv("GRIDMIND_BEDROCK_ENABLED", "1")
    with app_for_tests(db_store) as client:
        assert client.post(url).status_code == 401
        assert client.post(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/api/runs/unknown/explanation", headers=auth).status_code == 404
        assert client.post(f"/api/runs/{ACTIVE_ID}/explanation", headers=auth).status_code == 409
        model.assert_not_called()

        assert client.post(url, headers=auth, json={"prompt": "do not forward this"}).json() == result("unavailable")
        assert client.post(url, headers=auth).json() == result("unavailable")
        report = client.get(f"/api/reports?run_id={RUN_ID}", headers=auth)
        assert report.status_code == 200
        assert report.json()["totals"]["served_kwh"] == 25
        assert report.json()["trade_count"] == 1
    model.assert_called_once()
    assert "do not forward" not in repr(model.call_args)
