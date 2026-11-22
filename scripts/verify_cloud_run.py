"""Read back a single completed run independently from MySQL, DynamoDB and S3."""
import argparse
import json
from uuid import UUID

from sqlalchemy import select

from gridmind.cloud import CloudJournal, config
from gridmind.db import Run, Step, connect


def verify(run_id, factory, journal):
    run_id = str(UUID(run_id))
    with factory() as db:
        row = db.get(Run, run_id)
        if not row or row.status != "completed" or row.steps != row.completed_steps or row.steps != row.published_steps:
            raise ValueError("MySQL does not contain a fully published completed run.")
        steps = db.scalars(select(Step).where(Step.run_id == run_id).order_by(Step.step)).all()
        if [step.step for step in steps] != list(range(row.steps)):
            raise ValueError("MySQL step rows are missing or out of order.")
        report = journal.read_export(run_id)
        if (report.get("schema_version") != 1 or report.get("run_id") != row.id
                or report.get("scenario") != row.scenario or report.get("locality_id") != row.locality_id
                or report.get("step_count") != row.steps or len(report.get("events", [])) != row.steps):
            raise ValueError("S3 export does not match the MySQL run.")
        for step, exported in zip(steps, report["events"], strict=True):
            event = journal.read_step(run_id, step.step)
            if (event["run_id"] != step.run_id or event["step"] != step.step or event != exported
                    or event["timestamp"] != step.replay_at or event["grid"]["status"] != step.grid_status):
                raise ValueError(f"Persistence mismatch at step {step.step}.")
        return {"run_id": run_id, "mysql_steps": row.steps, "dynamodb_events": row.steps,
                "s3_export": journal.settings.object_key(run_id)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?", help="Run UUID from POST /api/runs or the market view")
    parser.add_argument("--check-schema", action="store_true", help="Verify existing DynamoDB account, region and keys")
    args = parser.parse_args()
    settings = config()
    if not settings:
        parser.error("Configure GRIDMIND_MODE=aws before checking the existing AWS resources.")
    if bool(args.run_id) == args.check_schema:
        parser.error("Choose exactly one of a run UUID or --check-schema.")
    journal = CloudJournal(settings)
    if args.check_schema:
        journal.schema()
        print("Existing DynamoDB table account, region and schema match.")
        return
    engine, factory = connect()
    try:
        if engine.dialect.name != "mysql" or engine.url.host not in (None, "localhost", "127.0.0.1", "::1"):
            parser.error("Use MySQL on this instance.")
        print(json.dumps(verify(args.run_id, factory, journal), indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
