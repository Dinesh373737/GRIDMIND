import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from .cloud import CloudJournal, config
from .db import ROOT, Outbox, Run, utcnow


def event_directory():
    return Path(os.environ.get("GRIDMIND_EVENT_DIR") or ROOT / ".runtime" / "events").resolve()


def event_path(run_id, step):
    return event_directory() / str(UUID(run_id)) / f"{step:08}.json"


def write_event(run_id, step, payload):
    path = event_path(run_id, step)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, allow_nan=False)
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != json.loads(encoded):
            raise ValueError("Event key already holds a different step")
        return
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as writer:
            writer.write(encoded)
            writer.flush()
            os.fsync(writer.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def publish(factory, run_id):
    with factory.begin() as db:
        run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if not run:
            return
        pending = db.scalars(select(Outbox).where(
            Outbox.run_id == run_id, Outbox.published_at.is_(None)
        ).order_by(Outbox.step)).all()
        settings = config()
        cloud = CloudJournal(settings) if settings and pending else None
        for item in pending:
            if item.step != run.published_steps or item.payload is None:
                raise ValueError("Step events must be delivered in order")
            write_event(run_id, item.step, item.payload)
            if cloud:
                try:
                    cloud.write_step(run_id, item.step, item.payload)
                    if item.step + 1 == run.steps:
                        events = read_events(run_id, item.step + 1, after=-1, limit=run.steps)
                        cloud.write_export(run_id, {
                            "schema_version": 1, "run_id": run_id, "scenario": run.scenario,
                            "locality_id": run.locality_id, "step_count": run.steps,
                            "events": [{**payload, "run_id": run_id, "step": step}
                                       for step, payload in enumerate(events)],
                        })
                except (ValueError, TypeError, KeyError):
                    raise
                except Exception as exc:
                    raise OSError("Cloud journal publication failed.") from exc
            item.published_at = utcnow()
            item.payload = None
            run.published_steps += 1


def read_events(run_id, published_steps, *, after, limit):
    return [json.loads(event_path(run_id, step).read_text(encoding="utf-8"))
            for step in range(after + 1, min(published_steps, after + 1 + limit))]
