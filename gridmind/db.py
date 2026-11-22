import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey, ForeignKeyConstraint,
    Index, Integer, Numeric, String, UniqueConstraint, create_engine, event, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

ROOT = Path(__file__).resolve().parent.parent


def uid():
    return str(uuid4())


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class FacilityType(Base):
    __tablename__ = "facility_types"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)


class ReplayDataset(Base):
    __tablename__ = "replay_datasets"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)


class ReplaySample(Base):
    __tablename__ = "replay_samples"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("replay_datasets.id"), primary_key=True)
    step: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_at: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (Index("ix_replay_sample_time", "dataset_id", "observed_at"),)


class Locality(Base):
    __tablename__ = "localities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    import_limit_kw: Mapped[float] = mapped_column(Float(53))
    island_capable: Mapped[bool] = mapped_column(Boolean)
    replay_pack: Mapped[str] = mapped_column(ForeignKey("replay_datasets.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (CheckConstraint("import_limit_kw > 0", name="ck_locality_limit"),)


class Participant(Base):
    __tablename__ = "participants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    locality_id: Mapped[str] = mapped_column(ForeignKey("localities.id"), index=True)
    facility_type_id: Mapped[str] = mapped_column(ForeignKey("facility_types.id"))
    name: Mapped[str] = mapped_column(String(100))
    base_load_kw: Mapped[float] = mapped_column(Float(53))
    essential_kw: Mapped[float] = mapped_column(Float(53))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        UniqueConstraint("locality_id", "name", name="uq_locality_participant"),
        CheckConstraint("base_load_kw >= 0 AND essential_kw >= 0 AND essential_kw <= base_load_kw",
                        name="ck_participant_load"),
    )


class Asset(Base):
    __tablename__ = "assets"
    participant_id: Mapped[str] = mapped_column(ForeignKey("participants.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(12), primary_key=True)
    power_kw: Mapped[float] = mapped_column(Float(53))
    energy_kwh: Mapped[float] = mapped_column(Float(53), default=0)
    __table_args__ = (
        CheckConstraint("kind IN ('solar','wind','battery')", name="ck_asset_kind"),
        CheckConstraint(
            "power_kw > 0 AND ((kind = 'battery' AND energy_kwh > 0) OR "
            "(kind <> 'battery' AND energy_kwh = 0))", name="ck_asset_rating",
        ),
    )


class Run(Base):
    __tablename__ = "simulation_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    locality_id: Mapped[str] = mapped_column(ForeignKey("localities.id"), index=True)
    active_locality_id: Mapped[str | None] = mapped_column(ForeignKey("localities.id"), unique=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("replay_datasets.id"))
    scenario: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    steps: Mapped[int] = mapped_column(Integer)
    completed_steps: Mapped[int] = mapped_column(Integer, default=0)
    published_steps: Mapped[int] = mapped_column(Integer, default=0)
    speed: Mapped[float] = mapped_column(Float(53))
    seed: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    latest: Mapped[dict | None] = mapped_column(JSON)
    worker_pid: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        CheckConstraint("published_steps >= 0 AND published_steps <= completed_steps "
                        "AND completed_steps <= steps AND steps > 0", name="ck_run_progress"),
        CheckConstraint("status IN ('queued','running','stopping','stopped','completed','failed','interrupted')",
                        name="ck_run_status"),
        CheckConstraint("scenario IN ('historical','stress','scheduled','reference')", name="ck_run_scenario"),
    )


class RunExplanation(Base):
    __tablename__ = "run_explanations"
    run_id: Mapped[str] = mapped_column(ForeignKey("simulation_runs.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(12))
    explanation: Mapped[str | None] = mapped_column(String(1800))
    claimed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        CheckConstraint("status IN ('pending','ready','unavailable')", name="ck_explanation_status"),
        CheckConstraint("(status = 'ready' AND explanation IS NOT NULL) OR "
                        "(status <> 'ready' AND explanation IS NULL)", name="ck_explanation_text"),
    )


class RunParticipant(Base):
    __tablename__ = "run_participants"
    run_id: Mapped[str] = mapped_column(ForeignKey("simulation_runs.id"), primary_key=True)
    participant_id: Mapped[str] = mapped_column(ForeignKey("participants.id"), primary_key=True)


class Order(Base):
    __tablename__ = "market_orders"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    step: Mapped[int] = mapped_column(Integer)
    participant_id: Mapped[str] = mapped_column(String(36))
    side: Mapped[str] = mapped_column(String(4))
    quantity_kwh: Mapped[float] = mapped_column(Numeric(15, 6))
    filled_kwh: Mapped[float] = mapped_column(Numeric(15, 6), default=0)
    price: Mapped[float] = mapped_column(Numeric(15, 6))
    priority: Mapped[str | None] = mapped_column(String(48))
    urgency: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    manual: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "participant_id"], ["run_participants.run_id", "run_participants.participant_id"]),
        UniqueConstraint("run_id", "step", "id", name="uq_order_scope"),
        CheckConstraint("side IN ('buy','sell')", name="ck_order_side"),
        CheckConstraint("quantity_kwh > 0 AND filled_kwh >= 0 AND filled_kwh <= quantity_kwh AND price >= 0",
                        name="ck_order_amounts"),
        Index("ix_order_run_step_status", "run_id", "step", "status"),
    )


class Trade(Base):
    __tablename__ = "market_trades"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    step: Mapped[int] = mapped_column(Integer)
    buy_order_id: Mapped[str] = mapped_column(String(100))
    sell_order_id: Mapped[str] = mapped_column(String(100))
    quantity_kwh: Mapped[float] = mapped_column(Numeric(15, 6))
    price: Mapped[float] = mapped_column(Numeric(15, 6))
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "step", "buy_order_id"], ["market_orders.run_id", "market_orders.step", "market_orders.id"]),
        ForeignKeyConstraint(["run_id", "step", "sell_order_id"], ["market_orders.run_id", "market_orders.step", "market_orders.id"]),
        CheckConstraint("buy_order_id <> sell_order_id AND quantity_kwh > 0 AND price >= 0", name="ck_trade_amounts"),
        Index("ix_trade_run_step", "run_id", "step"),
    )


class Step(Base):
    __tablename__ = "run_steps"
    run_id: Mapped[str] = mapped_column(ForeignKey("simulation_runs.id"), primary_key=True)
    step: Mapped[int] = mapped_column(Integer, primary_key=True)
    replay_at: Mapped[str] = mapped_column(String(40))
    grid_status: Mapped[str] = mapped_column(String(16))
    served_kwh: Mapped[float] = mapped_column(Float(53))
    unserved_kwh: Mapped[float] = mapped_column(Float(53))
    essential_served_kwh: Mapped[float] = mapped_column(Float(53))
    essential_unserved_kwh: Mapped[float] = mapped_column(Float(53))
    traded_kwh: Mapped[float] = mapped_column(Float(53))


class Outbox(Base):
    __tablename__ = "event_outbox"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    step: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict | None] = mapped_column(JSON)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (ForeignKeyConstraint(["run_id", "step"], ["run_steps.run_id", "run_steps.step"]),)


class Audit(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def database_url():
    direct = os.environ.get("GRIDMIND_DATABASE_URL")
    if direct:
        return direct
    local_config = ROOT / ".runtime" / "database.url"
    if local_config.exists():
        return local_config.read_text(encoding="utf-8-sig").strip()
    raise RuntimeError("Configure GRIDMIND_DATABASE_URL or run scripts/start.ps1 to initialize local MySQL.")


def connect(url=None):
    engine = create_engine(url or database_url(), pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys = ON")
    return engine, sessionmaker(engine, expire_on_commit=False)


def initialize(engine, factory):
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE OR REPLACE VIEW v_run_market AS "
            "SELECT run_id, COUNT(*) AS trade_count, SUM(quantity_kwh) AS energy_kwh, "
            "SUM(quantity_kwh * price) AS settlement_value FROM market_trades GROUP BY run_id"
            if engine.dialect.name == "mysql" else
            "CREATE VIEW IF NOT EXISTS v_run_market AS "
            "SELECT run_id, COUNT(*) AS trade_count, SUM(quantity_kwh) AS energy_kwh, "
            "SUM(quantity_kwh * price) AS settlement_value FROM market_trades GROUP BY run_id"
        )
    with factory.begin() as db:
        for key, name in (("household", "Household"), ("hospital", "Hospital"),
                          ("school", "School"), ("college", "College"),
                          ("producer", "Producer"), ("business", "Business")):
            if not db.get(FacilityType, key):
                db.add(FacilityType(id=key, name=name))


def save_assets(db, participant_id, *, solar_kw, wind_kw, battery_kw, battery_kwh):
    capacities = {"solar": (solar_kw, 0), "wind": (wind_kw, 0),
                  "battery": (battery_kw, battery_kwh)}
    for kind, (power, energy) in capacities.items():
        asset = db.get(Asset, (participant_id, kind))
        if power > 0:
            if asset:
                asset.power_kw, asset.energy_kwh = power, energy
            else:
                db.add(Asset(participant_id=participant_id, kind=kind, power_kw=power, energy_kwh=energy))
        elif asset:
            db.delete(asset)


def participant_records(db, locality_id):
    participants = db.execute(
        select(Participant, FacilityType.name).join(FacilityType)
        .where(Participant.locality_id == locality_id, Participant.active)
        .order_by(Participant.name)
    ).all()
    assets = db.scalars(
        select(Asset).join(Participant).where(Participant.locality_id == locality_id, Participant.active)
    )
    by_participant = {}
    for asset in assets:
        by_participant.setdefault(asset.participant_id, {})[asset.kind] = asset
    output = []
    for person, facility_name in participants:
        managed = by_participant.get(person.id, {})
        output.append({
            "id": person.id, "locality_id": person.locality_id, "name": person.name,
            "facility_type_id": person.facility_type_id, "facility_type": facility_name,
            "base_load_kw": person.base_load_kw, "essential_kw": person.essential_kw,
            "solar_kw": managed["solar"].power_kw if "solar" in managed else 0,
            "wind_kw": managed["wind"].power_kw if "wind" in managed else 0,
            "battery_kw": managed["battery"].power_kw if "battery" in managed else 0,
            "battery_kwh": managed["battery"].energy_kwh if "battery" in managed else 0,
        })
    return output
