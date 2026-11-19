import hashlib
import json
from dataclasses import asdict

from sqlalchemy import select

from .datasets import ReplayTick, load_replay, pack_demand_errors, replay_manifest, replay_options
from .db import Locality, Participant, ReplayDataset, ReplaySample, save_assets


def import_packs(factory):
    imported = []
    with factory.begin() as db:
        for option in replay_options():
            pack = option["id"]
            ticks = load_replay(pack)
            if not ticks:
                continue
            manifest = {
                **replay_manifest(pack), "interval_minutes": option["step_minutes"],
                "demand_error_factors": list(pack_demand_errors(ticks, pack)),
            }
            rows = [asdict(item) for item in ticks]
            digest = hashlib.sha256(
                json.dumps({"manifest": manifest, "rows": rows}, sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
            existing = db.get(ReplayDataset, pack)
            dataset_id = pack if not existing or existing.digest == digest else f"{pack}-{digest[:10]}"
            if not db.get(ReplayDataset, dataset_id):
                demand_source = "Austin meter profile" if manifest.get("local_meter_overlay") else "ERCOT demand profile"
                db.add(ReplayDataset(
                    id=dataset_id, name=f'{option["name"]} · {demand_source}', digest=digest,
                    manifest={**manifest, "steps": len(rows), "pack": pack},
                ))
                db.flush()
                db.add_all(ReplaySample(
                    dataset_id=dataset_id, step=i, observed_at=row["timestamp"], payload=row,
                ) for i, row in enumerate(rows))
            imported.append(dataset_id)
    return imported


def read_ticks(db, dataset_id):
    return tuple(ReplayTick(**row) for row in db.scalars(
        select(ReplaySample.payload).where(ReplaySample.dataset_id == dataset_id).order_by(ReplaySample.step)
    ))


def seed_demo(factory):
    with factory.begin() as db:
        if db.scalar(select(Locality.id).limit(1)):
            return
        source = db.get(ReplayDataset, "austin")
        if not source:
            return
        locality = Locality(name="Austin · Demo locality", import_limit_kw=65,
                            island_capable=True, replay_pack=source.id)
        db.add(locality)
        db.flush()
        roster = [
            ("Community hospital", "hospital", 39, 26, 0, 0, 0, 0),
            ("Neighbourhood school", "school", 24, 8, 0, 0, 0, 0),
            ("Riverside homes", "household", 12, 2, 6, 0, 0, 0),
            ("Solar and storage cooperative", "producer", 1, 0, 46, 0, 28, 70),
            ("Community wind cooperative", "producer", 1, 0, 0, 18, 0, 0),
        ]
        for name, kind, load, essential, solar, wind, battery_kw, battery_kwh in roster:
            person = Participant(locality_id=locality.id, facility_type_id=kind, name=name,
                                 base_load_kw=load, essential_kw=essential)
            db.add(person)
            db.flush()
            save_assets(db, person.id, solar_kw=solar, wind_kw=wind,
                        battery_kw=battery_kw, battery_kwh=battery_kwh)
