"""Opt-in AWS integrations. Local mode never imports AWS/JWT dependencies."""
import hashlib
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID


def encode(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} for GRIDMIND cloud mode.")
    return value


@dataclass(frozen=True)
class Cloud:
    region: str
    table: str
    bucket: str
    account_id: str
    hash_key: str
    range_key: str
    range_type: str
    prefix: str
    cognito_region: str
    user_pool_id: str
    client_id: str
    origins: tuple[str, ...]

    def key(self, run_id, step):
        return {
            self.hash_key: {"S": str(UUID(run_id))},
            self.range_key: {self.range_type: str(step) if self.range_type == "N" else f"{step:08}"},
        }

    def object_key(self, run_id):
        return f"{self.prefix}/{UUID(run_id)}.json"


def config():
    mode = os.environ.get("GRIDMIND_MODE", "local")
    if mode == "local":
        return None
    if mode != "aws":
        raise RuntimeError("GRIDMIND_MODE must be local or aws.")
    region = required("AWS_REGION")
    cognito_region = os.environ.get("GRIDMIND_COGNITO_REGION", region).strip()
    account = required("GRIDMIND_AWS_ACCOUNT_ID")
    pool = required("GRIDMIND_COGNITO_USER_POOL_ID")
    client = required("GRIDMIND_COGNITO_CLIENT_ID")
    origins = tuple(item.strip() for item in required("GRIDMIND_CORS_ORIGINS").split(",") if item.strip())
    if not origins or any(not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?", v) for v in origins):
        raise RuntimeError("Use explicit HTTPS frontend origins, without a trailing slash.")
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(re.escape(cognito_region) + r"_[a-zA-Z0-9]+", pool):
        raise RuntimeError("Invalid AWS account ID or Cognito pool region/ID.")
    if not re.fullmatch(r"[a-zA-Z0-9]+", client):
        raise RuntimeError("Invalid Cognito app client ID.")
    pk = os.environ.get("GRIDMIND_DDB_HASH_KEY", "run_id")
    sk = os.environ.get("GRIDMIND_DDB_RANGE_KEY", "step")
    sk_type = os.environ.get("GRIDMIND_DDB_RANGE_TYPE", "N")
    if pk in ("payload", "sha256") or sk in ("payload", "sha256") or not pk or pk == sk or not sk or sk_type not in ("N", "S"):
        raise RuntimeError("DynamoDB requires separate, nonreserved partition/sort keys and N or S sort type.")
    prefix = os.environ.get("GRIDMIND_EXPORT_PREFIX", "gridmind/runs").strip("/")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", prefix):
        raise RuntimeError("Invalid S3 export prefix.")
    return Cloud(region, required("GRIDMIND_DDB_TABLE"), required("GRIDMIND_EXPORT_BUCKET"), account,
                 pk, sk, sk_type, prefix, cognito_region, pool, client, origins)


@lru_cache(maxsize=4)
def aws_clients(region):
    import boto3
    from botocore.config import Config

    settings = Config(connect_timeout=3, read_timeout=5, retries={"mode": "standard", "total_max_attempts": 2})
    return (boto3.client("dynamodb", region_name=region, config=settings),
            boto3.client("s3", region_name=region, config=settings))


class CloudJournal:
    def __init__(self, settings, *, clients=None):
        self.settings = settings
        self.ddb, self.s3 = clients or aws_clients(settings.region)

    def schema(self):
        settings = self.settings
        table = self.ddb.describe_table(TableName=settings.table)["Table"]
        keys = {x["KeyType"]: x["AttributeName"] for x in table["KeySchema"]}
        types = {x["AttributeName"]: x["AttributeType"] for x in table["AttributeDefinitions"]}
        expected_arn = f"arn:aws:dynamodb:{settings.region}:{settings.account_id}:table/{settings.table}"
        if (table["TableArn"] != expected_arn or keys != {"HASH": settings.hash_key, "RANGE": settings.range_key}
                or types.get(settings.hash_key) != "S" or types.get(settings.range_key) != settings.range_type):
            raise ValueError("Existing DynamoDB table account, region or key schema does not match settings.")

    def read_step(self, run_id, step):
        settings = self.settings
        expected_arn = f"arn:aws:dynamodb:{settings.region}:{settings.account_id}:table/{settings.table}"
        data = self.ddb.get_item(TableName=expected_arn, Key=settings.key(run_id, step), ConsistentRead=True)
        item = data.get("Item")
        if not item:
            raise ValueError(f"Missing DynamoDB event at step {step}.")
        raw = item["payload"]["S"]
        if hashlib.sha256(raw.encode()).hexdigest() != item["sha256"]["S"]:
            raise ValueError(f"Invalid DynamoDB event checksum at step {step}.")
        payload = json.loads(raw)
        if payload["run_id"] != run_id or payload["step"] != step:
            raise ValueError(f"Invalid DynamoDB event identity at step {step}.")
        return payload

    def write_step(self, run_id, step, payload):
        from botocore.exceptions import ClientError

        settings = self.settings
        value = {**payload, "run_id": run_id, "step": step}
        raw = encode(value)
        # Leave space for the other item attributes in DynamoDB's 400 KB limit.
        if len(raw.encode()) > 390 * 1024:
            raise ValueError("GRIDMIND event exceeds the DynamoDB document limit.")
        expected_arn = f"arn:aws:dynamodb:{settings.region}:{settings.account_id}:table/{settings.table}"
        try:
            self.ddb.put_item(TableName=expected_arn, Item={
                **settings.key(run_id, step), "payload": {"S": raw},
                "sha256": {"S": hashlib.sha256(raw.encode()).hexdigest()},
            }, ConditionExpression="attribute_not_exists(#pk)", ExpressionAttributeNames={"#pk": settings.hash_key})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException" or self.read_step(run_id, step) != value:
                raise

    def read_export(self, run_id):
        settings = self.settings
        result = self.s3.get_object(Bucket=settings.bucket, Key=settings.object_key(run_id),
                                    ExpectedBucketOwner=settings.account_id)
        body = result["Body"]
        try:
            raw = body.read()
        finally:
            body.close()
        metadata = result["Metadata"]
        if metadata.get("sha256") != hashlib.sha256(raw).hexdigest() or metadata.get("run-id") != run_id:
            raise ValueError("Invalid S3 export identity or checksum.")
        document = json.loads(raw)
        if document["run_id"] != run_id:
            raise ValueError("Invalid S3 export run ID.")
        return document

    def write_export(self, run_id, document):
        from botocore.exceptions import ClientError

        settings = self.settings
        raw = encode(document).encode()
        checksum = hashlib.sha256(raw).hexdigest()
        try:
            self.s3.put_object(Bucket=settings.bucket, Key=settings.object_key(run_id), Body=raw,
                               ExpectedBucketOwner=settings.account_id, ContentType="application/json",
                               Metadata={"sha256": checksum, "run-id": run_id}, IfNoneMatch="*")
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("412", "PreconditionFailed") or self.read_export(run_id) != document:
                raise


@lru_cache(maxsize=8)
def jwks(issuer):
    from jwt import PyJWKClient

    return PyJWKClient(issuer + "/.well-known/jwks.json", timeout=5)


def verify_access_token(settings, token):
    import jwt

    issuer = f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/{settings.user_pool_id}"
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise jwt.InvalidTokenError("Invalid token header.")
    claims = jwt.decode(token, jwks(issuer).get_signing_key_from_jwt(token).key, algorithms=["RS256"], issuer=issuer,
                        options={"verify_aud": False, "require": ["exp", "iss", "sub", "client_id", "token_use"]})
    if claims["token_use"] != "access" or claims["client_id"] != settings.client_id:
        raise jwt.InvalidTokenError("Token must be an access token for this user-pool app client.")
    return claims
