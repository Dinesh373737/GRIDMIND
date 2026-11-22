"""Optional, one-shot summaries of completed runs using regional Bedrock inference."""

import json
import logging
from datetime import timedelta
from functools import lru_cache

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .db import RunExplanation, Step, Trade, utcnow

logger = logging.getLogger(__name__)
REGION = "us-east-1"
MODEL_ID = "amazon.nova-lite-v1:0"
MAX_EXPLANATION_CHARS = 1_800
PENDING_WINDOW = timedelta(seconds=30)
ENERGY_METRICS = ("served_kwh", "unserved_kwh", "essential_served_kwh", "essential_unserved_kwh", "traded_kwh")
SCENARIOS = {"historical", "stress", "scheduled", "reference"}
SYSTEM_PROMPT = (
    "Explain a completed microgrid simulation in 3 to 5 short plain-text sentences. "
    "Use only the supplied aggregates: describe grid disconnection, energy served and any shortfall, "
    "essential load, and local peer-to-peer trading. Islanded means disconnected from the upstream grid. "
    "Essential energy is part of the totals; trades move energy, they do not create it. "
    "A reference run disables peer-to-peer trading. Do not infer causes, comparisons, real-world outcomes, "
    "or recommended actions from these aggregates."
)


def explanation_result(status, explanation=None, *, cached=False):
    return {"status": status, "explanation": explanation, "cached": cached}


def cached_result(row):
    if row.status == "ready":
        return explanation_result("ready", row.explanation, cached=True)
    if row.status == "pending" and utcnow() - row.claimed_at < PENDING_WINDOW:
        return explanation_result("pending")
    return explanation_result("unavailable")


def claim_or_cached(factory, run_id):
    with factory() as db:
        row = db.get(RunExplanation, run_id)
        if row is not None:
            return cached_result(row)
    try:
        with factory.begin() as db:
            db.add(RunExplanation(run_id=run_id, status="pending"))
    except IntegrityError:
        # An independent transaction reads the winning claim after the loser rolls back.
        with factory() as db:
            row = db.get(RunExplanation, run_id)
            if row is None:
                raise
            return cached_result(row)
    # No database locks are held while waiting for AWS. An interrupted claim is
    # never recycled: the request could already have reached Bedrock and been billed.
    return None


def collect_summary(factory, run_id, scenario, expected_steps):
    trade_count = select(func.count()).select_from(Trade).where(Trade.run_id == run_id).scalar_subquery()
    islanded = Step.grid_status == "islanded"
    query = select(
        func.count().label("interval_count"),
        func.sum(case((islanded, 1), else_=0)).label("islanded_intervals"),
        func.sum(case((islanded, Step.unserved_kwh), else_=0)).label("islanded_unserved_kwh"),
        func.sum(case((islanded, Step.essential_unserved_kwh), else_=0)).label("islanded_essential_unserved_kwh"),
        *(func.sum(getattr(Step, key)).label(key) for key in ENERGY_METRICS),
        trade_count.label("trade_count"),
    ).where(Step.run_id == run_id)
    with factory() as db:
        values = db.execute(query).mappings().one()
    if scenario not in SCENARIOS or values["interval_count"] != expected_steps:
        raise ValueError("Completed run summary is inconsistent.")
    return {
        "scenario": scenario,
        **{key: round(float(value), 4) if key.endswith("_kwh") else int(value) for key, value in values.items()},
    }


@lru_cache(maxsize=1)
def bedrock_client():
    # Keep optional AWS dependencies out of the local/disabled path.
    from boto3.session import Session
    from botocore.config import Config

    session = Session()
    credentials = session.get_credentials()
    if credentials is None or credentials.method != "iam-role":
        raise RuntimeError("Bedrock requires the EC2 instance role.")
    return session.client("bedrock-runtime", region_name=REGION, config=Config(
        connect_timeout=2, read_timeout=8, retries={"mode": "standard", "total_max_attempts": 1},
        signature_version="v4", ignore_configured_endpoint_urls=True,
    ))


def generate_explanation(summary):
    body = json.dumps(summary, sort_keys=True, separators=(",", ":"), allow_nan=False)
    response = bedrock_client().converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": body}]}],
        inferenceConfig={"maxTokens": 220, "temperature": 0.2},
    )
    if response.get("stopReason") != "end_turn":
        raise ValueError("Incomplete Bedrock response.")
    blocks = response["output"]["message"]["content"]
    explanation = " ".join(block["text"] for block in blocks if isinstance(block.get("text"), str)).strip()
    if not explanation or len(explanation) > MAX_EXPLANATION_CHARS or "\x00" in explanation:
        raise ValueError("Invalid Bedrock explanation.")
    return explanation


def explain_completed_run(factory, run_id, scenario, steps):
    try:
        existing = claim_or_cached(factory, run_id)
        if existing is not None:
            return existing
        try:
            summary = collect_summary(factory, run_id, scenario, steps)
            explanation = generate_explanation(summary)
        except Exception as exc:
            # Never log the prompt, response, SDK exception message, or credentials.
            logger.warning("Run explanation unavailable (%s).", type(exc).__name__)
            explanation = None
        status = "ready" if explanation is not None else "unavailable"
        with factory.begin() as db:
            updated = db.execute(update(RunExplanation).where(
                RunExplanation.run_id == run_id, RunExplanation.status == "pending",
            ).values(status=status, explanation=explanation)).rowcount
        if updated == 1:
            return explanation_result(status, explanation)
    except SQLAlchemyError as exc:
        logger.warning("Run explanation cache unavailable (%s).", type(exc).__name__)
    return explanation_result("unavailable")
