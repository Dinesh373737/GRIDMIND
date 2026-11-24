"""Completed-run explanations with an opt-in, one-shot Groq enhancement."""

import asyncio
import json
import logging
import math
import os
import re

import httpx
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .db import GroqRunExplanation, Step, Trade

logger = logging.getLogger(__name__)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_ID = "openai/gpt-oss-20b"
MAX_COMPLETION_TOKENS = 400
MAX_RESPONSE_BYTES = 24_576
GROQ_DEADLINE_SECONDS = 8.0
MAX_EXPLANATION_CHARS = 1_800
ENERGY_METRICS = ("served_kwh", "unserved_kwh", "essential_served_kwh", "essential_unserved_kwh", "traded_kwh")
SCENARIOS = {
    "historical": "Historical replay",
    "stress": "Capacity stress",
    "scheduled": "Scheduled outage",
    "reference": "No-trading reference",
}
SYSTEM_PROMPT = (
    "Explain a completed microgrid simulation in 3 to 5 short plain-text sentences. Do not use Markdown. "
    "Use only the supplied aggregates: describe grid disconnection, energy served and any shortfall, "
    "essential load, and local peer-to-peer trading. Islanded means disconnected from the upstream grid. "
    "Essential energy is part of the totals; trades move energy, they do not create it. "
    "A reference run disables peer-to-peer trading. Do not infer causes, comparisons, real-world outcomes, "
    "or recommended actions from these aggregates."
)


def configured_groq_key():
    if os.environ.get("GRIDMIND_GROQ_ENABLED") != "1":
        return None
    key = os.environ.get("GROQ_API_KEY", "").strip()
    return key if re.fullmatch(r"[!-~]{1,512}", key) else None


def explanation_result(explanation=None, *, source=None, cached=False):
    return {"status": "ready" if explanation else "unavailable",
            "explanation": explanation, "cached": cached, "source": source}


def claim_or_cached(factory, run_id):
    with factory() as db:
        row = db.get(GroqRunExplanation, run_id)
        if row is not None:
            return False, row.explanation if row.status == "ready" else None
    try:
        with factory.begin() as db:
            db.add(GroqRunExplanation(run_id=run_id, status="pending"))
    except IntegrityError:
        # An independent transaction reads the winning claim after the loser rolls back.
        with factory() as db:
            row = db.get(GroqRunExplanation, run_id)
            if row is None:
                raise
            return False, row.explanation if row.status == "ready" else None
    # A claim is never recycled, even after process loss: inference could already
    # have been performed. No database transaction is open over the remote call.
    return True, None


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
    if any(value is None or not math.isfinite(value) or value < 0 for value in values.values()):
        raise ValueError("Completed run summary has invalid totals.")
    return {
        "scenario": scenario,
        **{key: round(float(value), 4) if key.endswith("_kwh") else int(value) for key, value in values.items()},
    }


def deterministic_explanation(summary):
    def energy(field):
        return f"{summary[field] or 0:,.4f}".rstrip("0").rstrip(".")

    total, islanded = summary["interval_count"], summary["islanded_intervals"]
    scenario = SCENARIOS[summary["scenario"]]
    if islanded:
        overview = f"{scenario}: the local grid was disconnected from the upstream grid for {islanded} of {total} intervals."
    else:
        overview = f"{scenario}: the local grid stayed connected for all {total} intervals."
    energy_totals = f"The simulation served {energy('served_kwh')} kWh and left {energy('unserved_kwh')} kWh unserved."
    if summary["essential_served_kwh"] or summary["essential_unserved_kwh"]:
        essential = (f"Of the essential demand, {energy('essential_served_kwh')} kWh was served and "
                     f"{energy('essential_unserved_kwh')} kWh was unserved.")
    else:
        essential = "The run recorded no essential demand."
    trades = summary["trade_count"]
    if trades:
        trade_label = "trade" if trades == 1 else "trades"
        market = f"The peer-to-peer market traded {energy('traded_kwh')} kWh across {trades} {trade_label}."
    else:
        market = ("Peer-to-peer trading was disabled in this reference run." if summary["scenario"] == "reference"
                  else "No peer-to-peer trades were recorded.")
    return " ".join((overview, energy_totals, essential, market))


def groq_client():
    return httpx.AsyncClient(
        timeout=httpx.Timeout(GROQ_DEADLINE_SECONDS, connect=2.0),
        follow_redirects=False, trust_env=False,
        transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False),
    )


async def request_groq(summary, key):
    body = json.dumps(summary, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": body}],
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "temperature": 0.2,
        "reasoning_effort": "low",
        "stream": False,
    }
    raw = bytearray()
    async with asyncio.timeout(GROQ_DEADLINE_SECONDS):
        async with groq_client() as client:
            async with client.stream("POST", GROQ_URL, json=payload, headers={
                "Authorization": f"Bearer {key}", "Accept": "application/json", "Accept-Encoding": "identity",
            }) as response:
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Unsupported provider response encoding.")
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("Provider response exceeded its size limit.")
                    raw.extend(chunk)
    choices = json.loads(raw)["choices"]
    if len(choices) != 1 or choices[0]["finish_reason"] != "stop":
        raise ValueError("Provider explanation was incomplete.")
    text = choices[0]["message"]["content"]
    if not isinstance(text, str):
        raise ValueError("Invalid provider explanation.")
    text = text.strip()
    if not text or len(text) > MAX_EXPLANATION_CHARS or "\x00" in text:
        raise ValueError("Invalid provider explanation.")
    # JSON can decode an escaped lone surrogate, but it is not valid UTF-8 and
    # cannot be persisted by the application database or sent as API text.
    text.encode("utf-8", errors="strict")
    return text


def explain_completed_run(factory, run_id, scenario, steps, *, groq_key=None):
    claimed = False
    if groq_key:
        try:
            claimed, cached = claim_or_cached(factory, run_id)
            if cached:
                return explanation_result(cached, source="groq", cached=True)
        except SQLAlchemyError as exc:
            logger.warning("Groq explanation cache unavailable (%s).", type(exc).__name__)

    try:
        summary = collect_summary(factory, run_id, scenario, steps)
    except (ValueError, TypeError, OverflowError, SQLAlchemyError) as exc:
        logger.warning("Run explanation summary unavailable (%s).", type(exc).__name__)
        return explanation_result()

    if claimed:
        try:
            explanation = asyncio.run(request_groq(summary, groq_key))
        except Exception as exc:
            # Provider response bodies, prompts, exception messages and headers are never logged.
            logger.warning("Groq explanation unavailable (%s).", type(exc).__name__)
            explanation = None
        status = "ready" if explanation is not None else "unavailable"
        try:
            with factory.begin() as db:
                updated = db.execute(update(GroqRunExplanation).where(
                    GroqRunExplanation.run_id == run_id, GroqRunExplanation.status == "pending",
                ).values(status=status, explanation=explanation)).rowcount
            if updated == 1 and explanation is not None:
                return explanation_result(explanation, source="groq")
        except SQLAlchemyError as exc:
            logger.warning("Groq explanation cache unavailable (%s).", type(exc).__name__)
    return explanation_result(deterministic_explanation(summary), source="deterministic")
