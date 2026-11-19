import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import aliased

from .datasets import load_ugim_benchmark
from .db import (
    ROOT, Asset, Audit, FacilityType, Locality, Order, Participant, ReplayDataset, Run, RunParticipant,
    Step, Trade, connect, initialize, participant_records, save_assets, uid,
)
from .journal import event_directory, publish, read_events
from .replay import import_packs, read_ticks, seed_demo
from .supervisor import Supervisor
from .worker import ACTIVE, calculate, finish, run_lock

logger = logging.getLogger(__name__)
METRICS = ("served_kwh", "unserved_kwh", "essential_served_kwh", "essential_unserved_kwh", "traded_kwh")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class LocalityInput(Input):
    name: str = Field(min_length=1, max_length=100)
    import_limit_kw: float = Field(gt=0, le=100000)
    island_capable: bool = True
    replay_pack: str = Field(default="austin", max_length=48)


class ParticipantInput(Input):
    locality_id: str = Field(max_length=36)
    facility_type_id: str = Field(max_length=24)
    name: str = Field(min_length=1, max_length=100)
    base_load_kw: float = Field(ge=0, le=100000)
    essential_kw: float = Field(default=0, ge=0)
    solar_kw: float = Field(default=0, ge=0, le=100000)
    wind_kw: float = Field(default=0, ge=0, le=100000)
    battery_kw: float = Field(default=0, ge=0, le=100000)
    battery_kwh: float = Field(default=0, ge=0, le=1000000)

    @model_validator(mode="after")
    def ratings(self):
        if self.essential_kw > self.base_load_kw:
            raise ValueError("Essential load must fit within base load.")
        if bool(self.battery_kw) != bool(self.battery_kwh):
            raise ValueError("A battery requires both a power rating and an energy capacity.")
        return self


class RunInput(Input):
    locality_id: str = Field(max_length=36)
    scenario: Literal["historical", "stress", "scheduled", "reference"] = "historical"
    steps: int = Field(default=24, ge=1, le=672)
    start_offset: int = Field(default=0, ge=0)
    speed: float = Field(default=1, ge=0.1, le=20)
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    outage_step: int | None = Field(default=None, ge=0)
    outage_duration: int = Field(default=4, ge=1, le=96)
    reference_scenario: Literal["historical", "stress", "scheduled"] = "historical"
    augment_weather: bool = False
    join_step: int | None = Field(default=None, ge=0)
    join_participant_ids: list[str] = Field(default_factory=list, max_length=3)


class OrderInput(Input):
    participant_id: str = Field(max_length=36)
    side: Literal["buy", "sell"]
    power_kw: float = Field(ge=0.004, le=100000)
    limit_price: float = Field(ge=0, le=100000)
    urgency: Literal["routine", "urgent"] = "routine"
    step: int | None = Field(default=None, ge=0)


def locality_view(locality):
    return {key: getattr(locality, key) for key in (
        "id", "name", "import_limit_kw", "island_capable", "replay_pack",
    )}


def run_view(run):
    fields = ("id", "locality_id", "scenario", "status", "steps", "completed_steps",
              "published_steps", "speed", "seed", "latest", "worker_pid", "error")
    return {**{key: getattr(run, key) for key in fields},
            "locality_name": run.snapshot["locality_name"], "created_at": run.created_at.isoformat() + "Z",
            "replay_pack": run.dataset_id, "participants": run.snapshot["participants"],
            "interval_minutes": run.snapshot["config"]["interval_minutes"]}


def order_view(order, name):
    return {
        "id": order.id, "step": order.step, "participant_id": order.participant_id,
        "participant_name": name, "side": order.side, "quantity_kwh": float(order.quantity_kwh),
        "filled_kwh": float(order.filled_kwh),
        "remaining_kwh": float(order.quantity_kwh - order.filled_kwh),
        "price": float(order.price), "priority": order.priority, "urgency": order.urgency,
        "status": order.status, "manual": order.manual, "created_at": order.created_at.isoformat() + "Z",
    }


def require_locality(db, locality_id, *, lock=False):
    query = select(Locality).where(Locality.id == locality_id, Locality.active)
    row = db.scalar(query.with_for_update() if lock else query)
    if not row:
        raise HTTPException(404, "Locality does not exist.")
    return row


def require_run(db, run_id, *, lock=False):
    row = run_lock(db, run_id) if lock else db.get(Run, run_id)
    if not row:
        raise HTTPException(404, "Simulation run does not exist.")
    return row


def require_pack(db, dataset_id):
    row = db.get(ReplayDataset, dataset_id)
    if not row:
        raise HTTPException(422, "Select an imported replay dataset.")
    return row


def create_app(url=None):
    @asynccontextmanager
    async def lifespan(app):
        engine, factory = connect(url)
        app.state.engine, app.state.factory = engine, factory
        initialize(engine, factory)
        import_packs(factory)
        seed_demo(factory)
        supervisor = app.state.supervisor = Supervisor(factory, engine.url.render_as_string(hide_password=False))
        supervisor.recover()
        event_directory().mkdir(parents=True, exist_ok=True)
        with factory() as db:
            unpublished = db.scalars(select(Run.id).where(Run.completed_steps > Run.published_steps)).all()
        for run_id in unpublished:
            try:
                publish(factory, run_id)
            except (OSError, ValueError):
                logger.exception("Could not recover step events for %s", run_id)

        async def monitor():
            while True:
                await asyncio.sleep(1)
                try:
                    await asyncio.to_thread(supervisor.reap)
                except OperationalError:
                    logger.warning("Local SQL connection unavailable during worker check")

        monitor_task = asyncio.create_task(monitor())
        try:
            yield
        finally:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
            await asyncio.to_thread(supervisor.close)
            engine.dispose()

    app = FastAPI(title="GRIDMIND", lifespan=lifespan)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        key = os.environ.get("GRIDMIND_API_KEY")
        if key and request.url.path.startswith("/api/") and request.url.path != "/api/health":
            received = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(key.encode(), received.encode()):
                return JSONResponse({"detail": "Enter a valid GRIDMIND access key."}, 401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(IntegrityError)
    async def constraint_error(_request, _error):
        return JSONResponse({"detail": "The write conflicts with an existing record or database constraint."}, 409)

    @app.exception_handler(OperationalError)
    async def database_error(_request, _error):
        return JSONResponse({"detail": "The SQL database is currently unavailable."}, 503)

    def db_factory():
        return app.state.factory

    def health_data():
        return {"sql": app.state.engine.dialect.name, "event_store": "local journal"}

    @app.get("/api/health")
    def health():
        with db_factory()() as db:
            db.execute(text("SELECT 1"))
        return health_data()

    @app.get("/api/bootstrap")
    def bootstrap():
        with db_factory()() as db:
            return {
                "health": health_data(),
                "packs": [{"id": d.id, "name": d.name, "steps": d.manifest["steps"],
                          "interval_minutes": d.manifest.get("interval_minutes", 15)}
                          for d in db.scalars(select(ReplayDataset).order_by(ReplayDataset.id))],
                "facility_types": [{"id": t.id, "name": t.name} for t in db.scalars(
                    select(FacilityType).order_by(FacilityType.name))],
                "localities": [locality_view(row) for row in db.scalars(
                    select(Locality).where(Locality.active).order_by(Locality.name))],
                "runs": [run_view(run) for run in db.scalars(select(Run).order_by(Run.created_at.desc()).limit(30))],
            }

    @app.get("/api/localities")
    def localities():
        with db_factory()() as db:
            return [locality_view(row) for row in db.scalars(
                select(Locality).where(Locality.active).order_by(Locality.name))]

    @app.post("/api/localities", status_code=201)
    @app.put("/api/localities/{locality_id}")
    def save_locality(body: LocalityInput, locality_id: str | None = None):
        with db_factory().begin() as db:
            require_pack(db, body.replay_pack)
            row = require_locality(db, locality_id, lock=True) if locality_id else Locality()
            for key, value in body.model_dump().items():
                setattr(row, key, value)
            db.add(row)
            db.flush()
            db.add(Audit(subject_id=row.id, action="locality_updated" if locality_id else "locality_created"))
            return locality_view(row)

    @app.delete("/api/localities/{locality_id}")
    def archive_locality(locality_id: str):
        with db_factory().begin() as db:
            row = require_locality(db, locality_id, lock=True)
            if db.scalar(select(Run.id).where(Run.active_locality_id == locality_id)):
                raise HTTPException(409, "Stop this locality's run before archiving it.")
            row.active = False
            db.add(Audit(subject_id=locality_id, action="locality_archived"))
        return {"id": locality_id, "archived": True}

    @app.get("/api/participants")
    def participants(locality_id: str):
        with db_factory()() as db:
            require_locality(db, locality_id)
            return participant_records(db, locality_id)

    @app.post("/api/participants", status_code=201)
    @app.put("/api/participants/{participant_id}")
    def save_participant(body: ParticipantInput, participant_id: str | None = None):
        with db_factory().begin() as db:
            require_locality(db, body.locality_id, lock=True)
            if not db.get(FacilityType, body.facility_type_id):
                raise HTTPException(422, "Select a valid facility type.")
            row = db.get(Participant, participant_id, with_for_update=True) if participant_id else Participant()
            if row is None or (participant_id and (not row.active or row.locality_id != body.locality_id)):
                raise HTTPException(404, "Participant does not exist in this locality.")
            ratings = {"solar_kw", "wind_kw", "battery_kw", "battery_kwh"}
            for key, value in body.model_dump(exclude=ratings).items():
                setattr(row, key, value)
            db.add(row)
            db.flush()
            save_assets(db, row.id, **body.model_dump(include=ratings))
            db.add(Audit(subject_id=row.id, action="participant_updated" if participant_id else "participant_created"))
            db.flush()
            return next(item for item in participant_records(db, body.locality_id) if item["id"] == row.id)

    @app.delete("/api/participants/{participant_id}")
    def archive_participant(participant_id: str):
        with db_factory().begin() as db:
            row = db.get(Participant, participant_id)
            if not row or not row.active:
                raise HTTPException(404, "Participant does not exist.")
            require_locality(db, row.locality_id, lock=True)
            row.active = False
            db.add(Audit(subject_id=participant_id, action="participant_archived"))
        return {"id": participant_id, "archived": True}

    @app.post("/api/runs", status_code=201)
    def start_run(body: RunInput):
        run_id = uid()
        with db_factory().begin() as db:
            locality = require_locality(db, body.locality_id, lock=True)
            dataset = require_pack(db, locality.replay_pack)
            if body.start_offset + body.steps > dataset.manifest["steps"]:
                raise HTTPException(422, "Run length exceeds the selected replay window.")
            if db.scalar(select(Run.id).where(Run.active_locality_id == locality.id)):
                raise HTTPException(409, "This locality already has an active simulation.")
            people = participant_records(db, locality.id)
            if not people:
                raise HTTPException(422, "Add a participant before starting the simulation.")
            join_ids = set(body.join_participant_ids)
            if join_ids and (body.join_step is None or not 0 < body.join_step < body.steps
                             or not join_ids.issubset({p["id"] for p in people})):
                raise HTTPException(422, "The joining participants and step must belong to this run.")
            for person in people:
                person.update(load_kw=person["base_load_kw"], kind=person["facility_type_id"],
                              island_capable=locality.island_capable,
                              grid_forming=person["battery_kw"] > 0,
                              active_from_step=body.join_step if person["id"] in join_ids else 0)
            outage_step = body.outage_step if body.outage_step is not None else min(6, body.steps - 1)
            if (body.scenario == "scheduled" or
                    body.scenario == "reference" and body.reference_scenario == "scheduled") and outage_step >= body.steps:
                raise HTTPException(422, "Choose an outage step within this run.")
            config = {
                "run_id": run_id, "scenario": body.scenario, "seed": body.seed,
                "interval_minutes": dataset.manifest.get("interval_minutes", 15),
                "demand_error_factors": dataset.manifest.get("demand_error_factors", []),
                "import_limit_kw": locality.import_limit_kw, "island_capable": locality.island_capable,
                "outage_step": outage_step, "outage_duration": body.outage_duration,
                "reference_scenario": body.reference_scenario,
                "augment_weather": body.augment_weather,
            }
            base_scenario = body.reference_scenario if body.scenario == "reference" else body.scenario
            if body.augment_weather and base_scenario != "stress":
                raise HTTPException(422, "Weather variability is available for capacity stress runs.")
            row = Run(id=run_id, locality_id=locality.id, active_locality_id=locality.id, dataset_id=dataset.id,
                      scenario=body.scenario, status="queued", steps=body.steps, speed=body.speed, seed=body.seed,
                      snapshot={"config": config, "locality_name": locality.name, "participants": people,
                                "start_offset": body.start_offset})
            db.add(row)
            db.flush()
            db.add_all(RunParticipant(run_id=row.id, participant_id=p["id"]) for p in people)
            db.add(Audit(subject_id=row.id, action="run_started"))
        try:
            app.state.supervisor.start(run_id)
        except OSError:
            with db_factory().begin() as db:
                finish(db, require_run(db, run_id, lock=True), "failed", "Could not start the locality worker.")
            raise HTTPException(503, "Could not start the locality worker.") from None
        return run_view(row)

    @app.get("/api/runs")
    def runs(locality_id: str | None = None, limit: int = Query(100, ge=1, le=300)):
        with db_factory()() as db:
            query = select(Run).order_by(Run.created_at.desc()).limit(limit)
            if locality_id:
                query = query.where(Run.locality_id == locality_id)
            return [run_view(row) for row in db.scalars(query)]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        with db_factory()() as db:
            return run_view(require_run(db, run_id))

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: str):
        with db_factory().begin() as db:
            run = require_run(db, run_id, lock=True)
            if run.status in ("queued", "running"):
                run.status = "stopping"
                db.add(Audit(subject_id=run.id, action="run_stop_requested"))
            return run_view(run)

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = Query(-1, ge=-1), limit: int = Query(100, ge=1, le=672)):
        with db_factory()() as db:
            published_steps = require_run(db, run_id).published_steps
        try:
            return read_events(run_id, published_steps, after=after, limit=limit)
        except (OSError, ValueError):
            raise HTTPException(503, "The locality event history is unavailable.") from None

    @app.get("/api/runs/{run_id}/outlook")
    def outlook(run_id: str):
        with db_factory()() as db:
            run = require_run(db, run_id)
            return run.latest["renewables"] if run.latest else []

    @app.get("/api/runs/{run_id}/preview")
    def preview(run_id: str):
        with db_factory()() as db:
            run = require_run(db, run_id, lock=True)
            if run.completed_steps >= run.steps:
                return run.latest
            ticks = read_ticks(db, run.dataset_id)
            errors = tuple(run.snapshot["config"].get("demand_error_factors", ()))
            return calculate(db, run, ticks, errors)[0]

    @app.get("/api/runs/{run_id}/orders")
    def orders(run_id: str, limit: int = Query(1000, ge=1, le=5000)):
        with db_factory()() as db:
            run = require_run(db, run_id)
            names = {p["id"]: p["name"] for p in run.snapshot["participants"]}
            rows = db.scalars(select(Order).where(Order.run_id == run_id)
                              .order_by(Order.step.desc(), Order.created_at.desc(), Order.id).limit(limit))
            return [order_view(row, names[row.participant_id]) for row in rows]

    @app.post("/api/runs/{run_id}/orders", status_code=201)
    def submit_order(run_id: str, body: OrderInput):
        with db_factory().begin() as db:
            run = require_run(db, run_id, lock=True)
            if run.status not in ("queued", "running"):
                raise HTTPException(409, "This simulation is not accepting new orders.")
            participant = next((p for p in run.snapshot["participants"] if p["id"] == body.participant_id), None)
            if not participant:
                raise HTTPException(422, "The participant must belong to this run's locality.")
            step = body.step if body.step is not None else run.completed_steps
            if not run.completed_steps <= step < run.steps or step < participant["active_from_step"]:
                raise HTTPException(409, "Choose an open interval when this participant is active.")
            found = db.scalar(select(Order.id).where(
                Order.run_id == run.id, Order.step == step, Order.participant_id == participant["id"],
                Order.side == body.side, Order.status == "queued",
            ))
            if found:
                raise HTTPException(409, "This participant already has an order on that side of this interval.")
            hours = run.snapshot["config"]["interval_minutes"] / 60
            row = Order(id=uid(), run_id=run.id, step=step, participant_id=participant["id"],
                        side=body.side, price=round(body.limit_price, 6),
                        quantity_kwh=round(body.power_kw * hours, 6), filled_kwh=0,
                        urgency=body.urgency, status="queued", manual=True)
            db.add(row)
            db.flush()
            return order_view(row, participant["name"])

    @app.post("/api/runs/{run_id}/orders/{order_id}/cancel")
    def cancel_order(run_id: str, order_id: str):
        with db_factory().begin() as db:
            run = require_run(db, run_id, lock=True)
            row = db.get(Order, order_id)
            if not row or row.run_id != run.id:
                raise HTTPException(404, "Order does not exist in this run.")
            if row.status != "queued":
                raise HTTPException(409, "Only queued orders can be cancelled.")
            row.status = "cancelled"
            name = next(p["name"] for p in run.snapshot["participants"] if p["id"] == row.participant_id)
            return order_view(row, name)

    @app.get("/api/runs/{run_id}/trades")
    def trades(run_id: str, limit: int = Query(1000, ge=1, le=5000)):
        with db_factory()() as db:
            run = require_run(db, run_id)
            names = {p["id"]: p["name"] for p in run.snapshot["participants"]}
            buy, sell = aliased(Order), aliased(Order)
            rows = db.execute(select(Trade, buy.participant_id, sell.participant_id)
                              .join(buy, Trade.buy_order_id == buy.id)
                              .join(sell, Trade.sell_order_id == sell.id)
                              .where(Trade.run_id == run_id).order_by(Trade.step.desc(), Trade.id).limit(limit))
            return [{"id": t.id, "step": t.step, "buyer_id": b, "seller_id": s, "buyer_name": names[b],
                     "seller_name": names[s], "quantity_kwh": float(t.quantity_kwh),
                     "price": float(t.price), "buy_order_id": t.buy_order_id, "sell_order_id": t.sell_order_id}
                    for t, b, s in rows]

    @app.get("/api/reports")
    def reports(run_id: str | None = None):
        with db_factory()() as db:
            totals = summarize_steps(db, run_id)
            market = db.execute(text(
                "SELECT SUM(trade_count), SUM(energy_kwh), SUM(settlement_value) "
                "FROM v_run_market WHERE (:run IS NULL OR run_id = :run)"
            ), {"run": run_id}).one()
            selected = require_run(db, run_id) if run_id else None
            return {
                "health": health_data(), "run_id": run_id,
                "totals": totals,
                "trade_count": int(market[0] or 0), "settled_kwh": float(market[1] or 0),
                "settlement_value": float(market[2] or 0),
                "counts": {label: db.scalar(select(func.count()).select_from(cls))
                           for label, cls in (("localities", Locality), ("participants", Participant),
                                              ("assets", Asset), ("runs", Run), ("orders", Order), ("trades", Trade))},
                "datasets": [{"id": d.id, "name": d.name, "digest": d.digest, **d.manifest}
                             for d in db.scalars(select(ReplayDataset))],
                "audit": [{"action": a.action, "subject_id": a.subject_id, "timestamp": a.created_at.isoformat() + "Z"}
                          for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(20))],
                "published_steps": selected.published_steps if selected else None,
            }

    def summarize_steps(db, run_id):
        query = select(func.count().label("step_count"),
                       *[func.sum(getattr(Step, key)).label(key) for key in METRICS])
        if run_id:
            query = query.where(Step.run_id == run_id)
        return {key: value or 0 for key, value in db.execute(query).mappings().one().items()}

    @app.get("/api/comparison")
    def comparison(run_id: str, reference_id: str):
        with db_factory()() as db:
            run, reference = require_run(db, run_id), require_run(db, reference_id)

            def matching_inputs(row):
                config = dict(row.snapshot["config"])
                scenario = config.pop("scenario")
                reference_scenario = config.pop("reference_scenario")
                config.pop("run_id")
                config.setdefault("augment_weather", False)
                manual = db.execute(select(
                    Order.step, Order.participant_id, Order.side, Order.quantity_kwh, Order.price, Order.urgency,
                ).where(Order.run_id == row.id, Order.manual, Order.status != "cancelled")
                    .order_by(Order.step, Order.created_at, Order.id)).all()
                return (row.locality_id, row.dataset_id, row.steps, row.snapshot["start_offset"],
                        row.snapshot["participants"], config, manual,
                        reference_scenario if scenario == "reference" else scenario)

            return {
                "comparable": run.status == reference.status == "completed" and run.scenario != "reference"
                and reference.scenario == "reference" and matching_inputs(run) == matching_inputs(reference),
                "runs": [{"id": item.id, "scenario": item.scenario,
                          "locality_name": item.snapshot["locality_name"], "totals": summarize_steps(db, item.id)}
                         for item in (run, reference)],
            }

    @app.get("/api/benchmarks/ugim")
    def benchmark():
        return load_ugim_benchmark() or {"available": False}

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(ROOT / "frontend" / "index.html")

    app.mount("/", StaticFiles(directory=ROOT / "frontend"), name="frontend")

    return app


app = create_app()
