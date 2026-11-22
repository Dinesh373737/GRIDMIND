from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from unittest.mock import patch

import pytest

from gridmind import journal
from gridmind.cloud import Cloud, config, encode
from gridmind.db import Locality, Outbox, ReplayDataset, Run, Step, connect, initialize
from scripts.verify_cloud_run import verify


def settings():
    return Cloud("ap-south-1", "gridmind-events", "gridmind-exports-el", "012345678901",
                 "run_id", "step", "N", "gridmind/runs", "ap-south-1", "ap-south-1_demo",
                 "demoClient", ("https://demo.example",))


class MemoryCloudJournal:
    def __init__(self):
        self.settings = settings()
        self.events = {}
        self.exports = {}
        self.fail = True

    def write_step(self, run_id, step, payload):
        value = {**payload, "run_id": run_id, "step": step}
        assert self.events.setdefault((run_id, step), value) == value

    def write_export(self, run_id, payload):
        if self.fail:
            self.fail = False
            raise OSError("Temporary mocked S3 failure")
        assert self.exports.setdefault(run_id, payload) == payload

    def read_step(self, run_id, step):
        return self.events[run_id, step]

    def read_export(self, run_id):
        return self.exports[run_id]


def test_local_is_default_and_aws_requires_explicit_settings(monkeypatch):
    monkeypatch.delenv("GRIDMIND_MODE", raising=False)
    assert config() is None
    monkeypatch.setenv("GRIDMIND_MODE", "aws")
    monkeypatch.delenv("AWS_REGION", raising=False)
    with pytest.raises(RuntimeError, match="AWS_REGION"):
        config()


def test_canonical_cloud_key_and_payload():
    run_id = str(uuid4())
    assert settings().key(run_id, 3) == {"run_id": {"S": run_id}, "step": {"N": "3"}}
    assert settings().object_key(run_id) == f"gridmind/runs/{run_id}.json"
    assert encode({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    with pytest.raises(ValueError):
        settings().object_key("../secret")


def test_cloud_outbox_retry_and_cross_store_verifier():
    run_id, locality_id = str(uuid4()), str(uuid4())
    cloud = MemoryCloudJournal()
    with TemporaryDirectory() as temp, patch.dict("os.environ", {"GRIDMIND_EVENT_DIR": str(Path(temp) / "events")}):
        engine, factory = connect("sqlite:///" + str(Path(temp) / "test.sqlite"))
        try:
            initialize(engine, factory)
            with factory.begin() as db:
                db.add(ReplayDataset(id="seed", name="Demo", digest="d" * 64, manifest={}))
                db.flush()
                db.add(Locality(id=locality_id, name="Demo", import_limit_kw=10, island_capable=True, replay_pack="seed"))
                db.flush()
                db.add(Run(id=run_id, locality_id=locality_id, dataset_id="seed", scenario="scheduled",
                           status="running", steps=2, speed=1, seed=42, snapshot={}, completed_steps=2))
                db.flush()
                for i in range(2):
                    timestamp = f"2024-01-01T00:{i * 15:02d}:00Z"
                    db.add(Step(run_id=run_id, step=i, replay_at=timestamp, grid_status="off",
                                served_kwh=1, unserved_kwh=0, essential_served_kwh=0,
                                essential_unserved_kwh=0, traded_kwh=1))
                    db.flush()
                    db.add(Outbox(run_id=run_id, step=i, payload={"timestamp": timestamp, "grid": {"status": "off"}}))

            with patch.object(journal, "config", return_value=settings()), patch.object(journal, "CloudJournal", return_value=cloud):
                with pytest.raises(OSError):
                    journal.publish(factory, run_id)
                with factory() as db:
                    assert db.get(Run, run_id).published_steps == 0
                    assert db.get(Outbox, (run_id, 0)).payload is not None
                journal.publish(factory, run_id)
                journal.publish(factory, run_id)

            with factory.begin() as db:
                assert db.get(Run, run_id).published_steps == 2
                db.get(Run, run_id).status = "completed"
            assert verify(run_id, factory, cloud) == {
                "run_id": run_id, "mysql_steps": 2, "dynamodb_events": 2,
                "s3_export": f"gridmind/runs/{run_id}.json",
            }
            cloud.events[run_id, 1] = {**cloud.events[run_id, 1], "timestamp": "bad"}
            with pytest.raises(ValueError, match="step 1"):
                verify(run_id, factory, cloud)
        finally:
            engine.dispose()


def test_dynamodb_and_s3_sdk_shape_and_readback():
    boto3 = pytest.importorskip("boto3")
    from botocore.response import StreamingBody
    from botocore.stub import Stubber
    from hashlib import sha256
    from io import BytesIO
    from gridmind.cloud import CloudJournal

    opts = dict(region_name="ap-south-1", aws_access_key_id="dummy", aws_secret_access_key="dummy")
    ddb, s3 = boto3.client("dynamodb", **opts), boto3.client("s3", **opts)
    cfg, run_id = settings(), str(uuid4())
    sink = CloudJournal(cfg, clients=(ddb, s3))
    table_arn = "arn:aws:dynamodb:ap-south-1:012345678901:table/gridmind-events"
    value = {"run_id": run_id, "step": 0, "grid": {"status": "off"}}
    raw = encode(value)
    item = {**cfg.key(run_id, 0), "payload": {"S": raw}, "sha256": {"S": sha256(raw.encode()).hexdigest()}}
    document = {"run_id": run_id, "events": [value]}
    body = encode(document).encode()
    metadata = {"run-id": run_id, "sha256": sha256(body).hexdigest()}
    key = cfg.object_key(run_id)
    common = dict(Bucket=cfg.bucket, Key=key, ExpectedBucketOwner=cfg.account_id)
    with Stubber(ddb) as dynamo, Stubber(s3) as bucket:
        dynamo.add_response("put_item", {}, {
            "TableName": table_arn, "Item": item, "ConditionExpression": "attribute_not_exists(#pk)",
            "ExpressionAttributeNames": {"#pk": cfg.hash_key},
        })
        dynamo.add_response("get_item", {"Item": item}, {
            "TableName": table_arn, "Key": cfg.key(run_id, 0), "ConsistentRead": True,
        })
        bucket.add_response("put_object", {}, {
            **common, "Body": body, "ContentType": "application/json", "Metadata": metadata, "IfNoneMatch": "*",
        })
        bucket.add_response("get_object", {
            "Body": StreamingBody(BytesIO(body), len(body)), "Metadata": metadata,
        }, common)
        sink.write_step(run_id, 0, value)
        assert sink.read_step(run_id, 0) == value
        sink.write_export(run_id, document)
        assert sink.read_export(run_id) == document
        dynamo.assert_no_pending_responses()
        bucket.assert_no_pending_responses()


def test_cognito_only_trusts_this_issuer_client_and_access_tokens():
    jwt = pytest.importorskip("jwt")
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from cryptography.hazmat.primitives.asymmetric import rsa
    from gridmind.cloud import verify_access_token

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cfg = settings()
    claims = {"iss": f"https://cognito-idp.{cfg.cognito_region}.amazonaws.com/{cfg.user_pool_id}",
              "exp": datetime.now(timezone.utc) + timedelta(minutes=5), "sub": "test-subject",
              "client_id": cfg.client_id, "token_use": "access"}
    mock_jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private.public_key()))

    def signed(payload):
        return jwt.encode(payload, private, algorithm="RS256", headers={"kid": "fixture"})

    with patch("gridmind.cloud.jwks", return_value=mock_jwks) as resolver:
        assert verify_access_token(cfg, signed(claims))["sub"] == "test-subject"
        resolver.assert_called_with(claims["iss"])
        for invalid in ({"client_id": "other"}, {"token_use": "id"}, {"iss": "https://other.example"},
                        {"exp": datetime.now(timezone.utc) - timedelta(minutes=5)}):
            with pytest.raises(jwt.InvalidTokenError):
                verify_access_token(cfg, signed({**claims, **invalid}))
