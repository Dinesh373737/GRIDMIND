import logging
import os
import sys
import time

from sqlalchemy import select, update

from .datasets import augment_weather, engine_frame
from .db import Order, Outbox, Run, Step, Trade, connect, utcnow
from .engine import tick
from .journal import publish
from .replay import read_ticks

logger = logging.getLogger(__name__)
ACTIVE = {"queued", "running", "stopping"}


def run_lock(db, run_id):
    return db.scalar(select(Run).where(Run.id == run_id).with_for_update())


def finish(db, run, status, error=None):
    run.status = status
    run.error = error
    run.active_locality_id = None
    run.finished_at = utcnow()
    db.execute(update(Order).where(Order.run_id == run.id, Order.status == "queued").values(status="cancelled"))


def orders_for(db, run_id, step):
    return db.scalars(select(Order).where(
        Order.run_id == run_id, Order.step == step, Order.status == "queued",
    ).order_by(Order.created_at, Order.id)).all()


def calculate(db, run, ticks, errors):
    index = run.completed_steps
    offset = run.snapshot["start_offset"] + index
    frame = engine_frame(ticks, offset, demand_errors=errors)
    frame["step"] = index
    if run.snapshot["config"].get("augment_weather"):
        frame = augment_weather(frame, run.seed, run.snapshot["config"]["interval_minutes"])
    roster = run.snapshot["participants"]
    pending = orders_for(db, run.id, index)
    orders = [{
        "id": order.id, "participant_id": order.participant_id, "side": order.side,
        "quantity_kwh": float(order.quantity_kwh), "price": float(order.price),
        "urgency": order.urgency, "submitted_at": order.created_at.isoformat(),
    } for order in pending]
    previous = run.latest["state"] if run.latest else None
    return tick(run.snapshot["config"], frame, roster, orders, previous), {o.id: o for o in pending}


def settle(db, run, result, pending):
    index = run.completed_steps
    for item in result["market"]["orders"]:
        existing = pending.pop(item["id"], None)
        if existing is None:
            existing = Order(
                id=item["id"], run_id=run.id, step=index, participant_id=item["participant_id"],
                side=item["side"], quantity_kwh=round(item["quantity_kwh"], 6),
                price=round(item["price"], 6), manual=False, urgency=item["urgency"],
            )
            db.add(existing)
        existing.filled_kwh = round(item["filled_kwh"], 6)
        existing.status = item["status"]
        existing.priority = str(item["priority"])[:48] if item["priority"] is not None else None
    for order in pending.values():
        order.status = "unavailable"
    db.flush()
    db.add_all(Trade(
        id=fill["id"], run_id=run.id, step=index, buy_order_id=fill["buy_order_id"],
        sell_order_id=fill["sell_order_id"], quantity_kwh=round(fill["quantity_kwh"], 6),
        price=round(fill["price"], 6),
    ) for fill in result["market"]["fills"])
    totals = result["totals"]
    db.add(Step(
        run_id=run.id, step=index, replay_at=result["timestamp"], grid_status=result["grid"]["status"],
        served_kwh=totals["served_kwh"], unserved_kwh=totals["unserved_kwh"],
        essential_served_kwh=totals["essential_served_kwh"],
        essential_unserved_kwh=totals["essential_unserved_kwh"], traded_kwh=totals["traded_kwh"],
    ))
    db.flush()
    db.add(Outbox(run_id=run.id, step=index, payload=result))
    run.latest = result
    run.completed_steps += 1


def deliver(factory, run_id):
    for attempt in range(3):
        try:
            publish(factory, run_id)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.1 * (attempt + 1))


def wait_for_tick(factory, run_id, next_at):
    while (remaining := next_at - time.monotonic()) > 0:
        time.sleep(min(remaining, 0.2))
        with factory() as db:
            status = db.scalar(select(Run.status).where(Run.id == run_id))
        if status != "running":
            break


def work(run_id):
    engine, factory = connect()
    try:
        with factory.begin() as db:
            run = run_lock(db, run_id)
            if not run:
                return
            if run.status == "stopping":
                finish(db, run, "stopped")
                return
            if run.status != "queued":
                return
            ticks = read_ticks(db, run.dataset_id)
            errors = tuple(run.snapshot["config"].get("demand_error_factors", ()))
            speed = run.speed
            run.status, run.worker_pid = "running", os.getpid()
        while True:
            next_at = time.monotonic() + 1 / speed
            with factory.begin() as db:
                run = run_lock(db, run_id)
                if not run or run.status not in ACTIVE:
                    return
                if run.status == "stopping":
                    finish(db, run, "stopped")
                    return
                result, pending = calculate(db, run, ticks, errors)
                settle(db, run, result, pending)
            deliver(factory, run_id)
            with factory.begin() as db:
                run = run_lock(db, run_id)
                if run.status == "stopping":
                    finish(db, run, "stopped")
                    return
                if run.status != "running":
                    return
                if run.completed_steps == run.steps:
                    finish(db, run, "completed")
                    return
            wait_for_tick(factory, run_id, next_at)
    except Exception as exc:
        logger.exception("Simulation worker failed for %s", run_id)
        with factory.begin() as db:
            run = run_lock(db, run_id)
            if run and run.status in ACTIVE:
                finish(db, run, "failed", f"Simulation worker failed ({type(exc).__name__}). Check the local worker log.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    work(sys.argv[1])
