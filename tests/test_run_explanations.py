import asyncio
import json
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from gridmind import api, explanations
from gridmind.db import (
    GroqRunExplanation, Locality, Order, Participant, ReplayDataset, Run, RunExplanation, RunParticipant, Step, Trade,
    connect, initialize, utcnow,
)

RUN_ID = "private-completed-run-id"
ACTIVE_ID = "private-active-run-id"
LOCALITY_ID = "private-locality-id"
TEXT = "The grid disconnected for one interval. The local market traded 2.5 kWh."


TEST_KEY = "unit-test-credential-not-secret"
FALLBACK = (
    "Scheduled outage: the local grid was disconnected from the upstream grid for 1 of 2 intervals. "
    "The simulation served 25 kWh and left 3 kWh unserved. "
    "Of the essential demand, 10 kWh was served and 0.5 kWh was unserved. "
    "The peer-to-peer market traded 2.5 kWh across 1 trade."
)
AGGREGATES = {
    "scenario": "scheduled", "interval_count": 2, "islanded_intervals": 1,
    "served_kwh": 25, "unserved_kwh": 3, "essential_served_kwh": 10, "essential_unserved_kwh": 0.5,
    "traded_kwh": 2.5, "islanded_unserved_kwh": 3, "islanded_essential_unserved_kwh": 0.5,
    "trade_count": 1,
}


def result(source, explanation=FALLBACK, *, cached=False):
    return {"status": "ready" if explanation else "unavailable",
            "explanation": explanation, "cached": cached, "source": source}


def model_output(text, reason="stop"):
    return {"choices": [{"finish_reason": reason, "message": {
        "role": "assistant", "content": text, "reasoning": "do not show this reasoning",
    }}]}


@pytest.fixture(autouse=True)
def forbid_live_groq(monkeypatch):
    async def blocked(*_args, **_kwargs):
        pytest.fail("Tests must not use the network transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)


@pytest.fixture
def mocked_transport(monkeypatch):
    def install(handler):
        requests, options = [], []

        def make_transport(**kwargs):
            options.append(kwargs)

            async def dispatch(request):
                requests.append(request)
                reply = handler(request)
                return await reply if asyncio.iscoroutine(reply) else reply

            return httpx.MockTransport(dispatch)

        monkeypatch.setattr(httpx, "AsyncHTTPTransport", make_transport)
        return requests, options

    return install


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


def test_cache_persists_and_only_aggregate_fields_reach_groq(db_store, monkeypatch, mocked_transport):
    url, _, factory = db_store
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted-proxy.invalid")
    requests, options = mocked_transport(lambda _req: httpx.Response(200, json=model_output(f" {TEXT}\n")))

    assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 2, groq_key=TEST_KEY) == result("groq", TEXT)
    assert options == [{"retries": 0, "trust_env": False}]
    request, = requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert request.headers["Accept-Encoding"] == "identity"
    params = json.loads(request.content)
    assert params["model"] == "openai/gpt-oss-20b"
    assert params["max_completion_tokens"] == 400
    assert params["reasoning_effort"] == "low" and params["stream"] is False
    assert [message["role"] for message in params["messages"]] == ["system", "user"]
    body = params["messages"][1]["content"]
    assert len(body) < 400 and json.loads(body) == AGGREGATES
    assert "private" not in json.dumps(params).lower()
    assert TEST_KEY.encode() not in request.content
    assert RUN_ID.encode() not in request.content and b"2018-08" not in request.content

    restarted_engine, restarted_factory = connect(url)
    try:
        assert explanations.explain_completed_run(restarted_factory, RUN_ID, "scheduled", 2, groq_key=TEST_KEY) == result(
            "groq", TEXT, cached=True)
        assert explanations.explain_completed_run(restarted_factory, RUN_ID, "scheduled", 2) == result("deterministic")
        assert len(requests) == 1
    finally:
        restarted_engine.dispose()


def test_concurrent_claims_only_invoke_groq_once_and_do_not_block_fallback(
    db_store, monkeypatch, mocked_transport,
):
    _, _, factory = db_store
    before_insert = Barrier(2)
    invoke = Event()
    release_model = Event()

    def synchronize_claims(session, *_):
        if any(isinstance(row, GroqRunExplanation) for row in session.new):
            before_insert.wait(timeout=5)

    async def model_reply(_request):
        invoke.set()
        assert await asyncio.to_thread(release_model.wait, 5)
        return httpx.Response(200, json=model_output(TEXT))

    event.listen(factory, "before_flush", synchronize_claims)
    requests, _ = mocked_transport(model_reply)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = {
                workers.submit(explanations.explain_completed_run, factory, RUN_ID, "scheduled", 2, groq_key=TEST_KEY)
                for _ in range(2)
            }
            try:
                assert invoke.wait(timeout=5)
                # Both initial reads saw no cache. The losing INSERT must finish
                # with a fallback while the winner still waits on the provider.
                done, running = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
                assert len(done) == len(running) == 1
                assert done.pop().result() == result("deterministic")
            finally:
                release_model.set()
            assert running.pop().result(timeout=5) == result("groq", TEXT)
    finally:
        event.remove(factory, "before_flush", synchronize_claims)
    assert len(requests) == 1


@pytest.mark.parametrize("failure", [
    "connect", "rate", "redirect", "server", "oversize", "invalid_json", "invalid_unicode",
    "truncated", "empty", "text_oversize",
])
def test_groq_failure_falls_back_and_is_never_retried(db_store, mocked_transport, caplog, failure):
    _, _, factory = db_store

    def failure_reply(request):
        if failure == "connect":
            raise httpx.ConnectError("internal-provider-detail", request=request)
        if failure == "rate":
            return httpx.Response(429, json={"message": "internal-provider-detail"}, headers={"Retry-After": "1"})
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://untrusted-redirect.invalid/"})
        if failure == "server":
            return httpx.Response(503)
        if failure == "oversize":
            return httpx.Response(200, content=b" " * (explanations.MAX_RESPONSE_BYTES + 1))
        if failure == "invalid_json":
            return httpx.Response(200, content=b"not JSON")
        if failure == "invalid_unicode":
            return httpx.Response(200, content=json.dumps(model_output("\ud800"), ensure_ascii=True).encode("ascii"))
        if failure == "empty":
            return httpx.Response(200, json=model_output(None))
        if failure == "text_oversize":
            return httpx.Response(200, json=model_output("x" * (explanations.MAX_EXPLANATION_CHARS + 1)))
        return httpx.Response(200, json=model_output(TEXT, reason="length"))

    requests, _ = mocked_transport(failure_reply)
    for _ in range(2):
        assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 2, groq_key=TEST_KEY) == result("deterministic")
    assert len(requests) == 1
    assert "internal-provider-detail" not in caplog.text and TEST_KEY not in caplog.text
    with factory() as db:
        row = db.get(GroqRunExplanation, RUN_ID)
        assert row.status == "unavailable" and row.explanation is None


def test_total_deadline_cancels_stalled_http_once(db_store, monkeypatch, mocked_transport):
    _, _, factory = db_store
    monkeypatch.setattr(explanations, "GROQ_DEADLINE_SECONDS", 0.03)

    async def stalled(_request):
        await asyncio.sleep(10)
        pytest.fail("provider should have timed out")

    requests, _ = mocked_transport(stalled)
    start = time.monotonic()
    for _ in range(2):
        assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 2, groq_key=TEST_KEY) == result("deterministic")
    assert len(requests) == 1 and time.monotonic() - start < 2


def test_stale_claim_is_never_recycled_even_after_process_loss(db_store):
    _, _, factory = db_store
    with factory.begin() as db:
        db.add(GroqRunExplanation(run_id=RUN_ID, status="pending", claimed_at=utcnow() - timedelta(hours=1)))
    for _ in range(2):
        assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 2, groq_key=TEST_KEY) == result("deterministic")
    with factory() as db:
        assert db.get(GroqRunExplanation, RUN_ID).status == "pending"


def test_legacy_bedrock_failure_does_not_block_groq(db_store, mocked_transport):
    _, _, factory = db_store
    with factory.begin() as db:
        db.add(RunExplanation(run_id=RUN_ID, status="unavailable"))
    requests, _ = mocked_transport(lambda _req: httpx.Response(200, json=model_output(TEXT)))
    assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 2, groq_key=TEST_KEY) == result("groq", TEXT)
    assert len(requests) == 1
    with factory() as db:
        assert db.get(RunExplanation, RUN_ID).status == "unavailable"
        assert db.get(GroqRunExplanation, RUN_ID).explanation == TEXT


def test_missing_cache_table_falls_back_without_provider(db_store):
    _, engine, factory = db_store
    GroqRunExplanation.__table__.drop(engine)
    assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 2, groq_key=TEST_KEY) == result("deterministic")
    assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 2) == result("deterministic")


def test_no_summary_returns_unavailable_without_provider(db_store):
    _, _, factory = db_store
    assert explanations.explain_completed_run(factory, RUN_ID, "scheduled", 3, groq_key=TEST_KEY) == result(None, None)


def test_empty_essential_and_market_are_not_reported_as_percentages():
    reference = {
        **AGGREGATES, "scenario": "reference", "islanded_intervals": 0,
        "essential_served_kwh": 0, "essential_unserved_kwh": 0, "trade_count": 0, "traded_kwh": 0,
    }
    explanation = explanations.deterministic_explanation(reference)
    assert "stayed connected for all 2 intervals" in explanation
    assert "recorded no essential demand" in explanation
    assert "trading was disabled in this reference run" in explanation
    assert "%" not in explanation


def test_groq_table_compiles_for_mysql_without_legacy_table_changes():
    ddl = str(CreateTable(GroqRunExplanation.__table__).compile(dialect=mysql.dialect()))
    assert "CREATE TABLE groq_run_explanations" in ddl
    assert "PRIMARY KEY (run_id)" in ddl and "FOREIGN KEY(run_id) REFERENCES simulation_runs (id)" in ddl
    assert RunExplanation.__tablename__ == "run_explanations"


@contextmanager
def app_for_tests(db_store):
    _, engine, factory = db_store
    app = api.create_app()
    # Route tests use a real isolated database but no production AWS/SQL lifespan.
    app.state.engine, app.state.factory = engine, factory
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize(("flag", "key"), [(None, None), (None, TEST_KEY), ("1", None), ("1", "not a token")])
def test_unauthorized_and_no_opt_in_or_key_stay_offline(db_store, monkeypatch, flag, key):
    for name, value in (("GRIDMIND_GROQ_ENABLED", flag), ("GROQ_API_KEY", key)):
        monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
    monkeypatch.setenv("GRIDMIND_BEDROCK_ENABLED", "1")
    monkeypatch.setenv("GRIDMIND_API_KEY", "test-key")
    monkeypatch.setattr(api, "cloud_config", lambda: None)
    with app_for_tests(db_store) as client:
        url = f"/api/runs/{RUN_ID}/explanation"
        assert client.post(url).status_code == 401
        assert client.post(url, headers={"X-API-Key": "test-key"}).json() == result("deterministic")
    with db_store[2]() as db:
        assert db.get(GroqRunExplanation, RUN_ID) is None


def test_explicit_local_opt_in_uses_groq(db_store, monkeypatch, mocked_transport):
    monkeypatch.setenv("GRIDMIND_GROQ_ENABLED", "1")
    monkeypatch.setenv("GROQ_API_KEY", TEST_KEY)
    monkeypatch.setenv("GRIDMIND_API_KEY", "test-key")
    monkeypatch.setattr(api, "cloud_config", lambda: None)
    requests, _ = mocked_transport(lambda _req: httpx.Response(200, json=model_output(TEXT)))
    with app_for_tests(db_store) as client:
        url = f"/api/runs/{RUN_ID}/explanation"
        assert client.post(url).status_code == 401
        assert client.post(url, headers={"X-API-Key": "test-key"}).json() == result("groq", TEXT)
    assert len(requests) == 1


def test_cloud_auth_completion_gate_and_unchanged_report_fallback(db_store, monkeypatch, mocked_transport):
    jwt = pytest.importorskip("jwt")
    monkeypatch.setattr(api, "cloud_config", lambda: SimpleNamespace(origins=("https://demo.example",)))

    def verify(_cloud, token):
        if token != "access-token":
            raise jwt.InvalidTokenError("invalid")

    monkeypatch.setattr(api, "verify_access_token", verify)
    monkeypatch.setenv("GRIDMIND_GROQ_ENABLED", "1")
    monkeypatch.setenv("GROQ_API_KEY", TEST_KEY)
    requests, _ = mocked_transport(lambda _req: httpx.Response(429))
    auth = {"Authorization": "Bearer access-token"}
    url = f"/api/runs/{RUN_ID}/explanation"

    with app_for_tests(db_store) as client:
        assert client.post(url).status_code == 401
        assert client.post(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/api/runs/unknown/explanation", headers=auth).status_code == 404
        assert client.post(f"/api/runs/{ACTIVE_ID}/explanation", headers=auth).status_code == 409
        assert not requests

        assert client.post(url, headers=auth, json={"prompt": "do not forward this"}).json() == result("deterministic")
        assert client.post(url, headers=auth).json() == result("deterministic")
        report = client.get(f"/api/reports?run_id={RUN_ID}", headers=auth)
        assert report.status_code == 200
        assert report.json()["totals"]["served_kwh"] == 25
        assert report.json()["trade_count"] == 1
    assert len(requests) == 1 and b"do not forward" not in requests[0].content
