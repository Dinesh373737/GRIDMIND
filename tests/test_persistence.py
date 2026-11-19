from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from gridmind import journal, worker
from gridmind.db import Locality, Order, Outbox, Participant, ReplayDataset, Run, RunParticipant, Step, Trade
from gridmind.db import connect, initialize, uid
from gridmind.engine import tick


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        settings = patch.dict(os.environ, {"GRIDMIND_EVENT_DIR": str(directory / "events")})
        settings.start()
        self.addCleanup(settings.stop)
        engine, self.factory = connect(f"sqlite:///{(directory / 'simulation.sqlite').as_posix()}")
        self.addCleanup(engine.dispose)
        initialize(engine, self.factory)

        self.run_id, locality_id, self.buyer_id, seller_id = (uid() for _ in range(4))
        self.pending_order_id = uid()
        config = {"run_id": self.run_id, "scenario": "historical", "interval_minutes": 15}
        with self.factory.begin() as db:
            db.add(ReplayDataset(id="fixture", name="Persistence test", digest="a" * 64, manifest={}))
            db.flush()
            db.add(Locality(id=locality_id, name="Test locality", import_limit_kw=60,
                            island_capable=True, replay_pack="fixture"))
            db.flush()
            db.add_all((
                Participant(id=self.buyer_id, locality_id=locality_id, facility_type_id="hospital",
                            name="Hospital", base_load_kw=36, essential_kw=36),
                Participant(id=seller_id, locality_id=locality_id, facility_type_id="producer",
                            name="Seller", base_load_kw=0, essential_kw=0),
                Run(id=self.run_id, locality_id=locality_id, active_locality_id=locality_id,
                    dataset_id="fixture", scenario="historical", status="running", steps=1,
                    speed=1, seed=42, snapshot={"config": config}),
            ))
            db.flush()
            db.add_all(RunParticipant(run_id=self.run_id, participant_id=person_id)
                       for person_id in (self.buyer_id, seller_id))
            db.flush()
            db.add(Order(id=self.pending_order_id, run_id=self.run_id, step=0,
                         participant_id=self.buyer_id, side="buy", quantity_kwh=9,
                         filled_kwh=0, price=12, urgency="urgent", manual=True, status="queued"))

        frame = {"step": 0, "timestamp": "2018-08-24T21:00:00+00:00", "outage": True, "solar_factor": 1}
        roster = [
            {"id": self.buyer_id, "name": "Hospital", "load_kw": 36, "essential_kw": 36},
            {"id": seller_id, "name": "Seller", "solar_kw": 48},
        ]
        orders = [{"id": self.pending_order_id, "participant_id": self.buyer_id, "side": "buy",
                   "quantity_kwh": 9, "price": 12, "urgency": "urgent"}]
        self.result = tick(config, frame, roster, orders)
        self.assertEqual(len(self.result["market"]["fills"]), 1)

    def settle(self):
        with self.factory.begin() as db:
            run = worker.run_lock(db, self.run_id)
            pending = {row.id: row for row in worker.orders_for(db, self.run_id, 0)}
            worker.settle(db, run, self.result, pending)

    def test_invalid_trade_rolls_back_order_updates_and_step(self):
        # The order update and auto offer are flushed before the invalid trade's FK fails.
        self.result["market"]["fills"][0]["sell_order_id"] = uid()
        with self.assertRaises(IntegrityError):
            self.settle()

        with self.factory() as db:
            original = db.get(Order, self.pending_order_id)
            self.assertEqual((original.status, original.filled_kwh), ("queued", 0))
            self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 1)
            for model in (Trade, Step, Outbox):
                self.assertEqual(db.scalar(select(func.count()).select_from(model)), 0)
            run = db.get(Run, self.run_id)
            self.assertEqual((run.completed_steps, run.published_steps, run.latest), (0, 0, None))
        self.assertFalse(journal.event_path(self.run_id, 0).exists())

    def test_durable_event_recovers_after_failed_publication_commit(self):
        self.settle()
        write_event = journal.write_event

        def fail_after_writing(run_id, step, payload):
            write_event(run_id, step, payload)
            raise OSError("Simulated interruption after writing event")

        with patch.object(journal, "write_event", side_effect=fail_after_writing):
            with self.assertRaises(OSError):
                journal.publish(self.factory, self.run_id)

        event_file = journal.event_path(self.run_id, 0)
        written_at = event_file.stat().st_mtime_ns
        with self.factory() as db:
            run = db.get(Run, self.run_id)
            queued = db.get(Outbox, (self.run_id, 0))
            self.assertEqual((run.completed_steps, run.published_steps), (1, 0))
            self.assertIsNone(queued.published_at)
            self.assertIsNotNone(queued.payload)
            self.assertEqual(journal.read_events(self.run_id, run.published_steps, after=-1, limit=2), [])

        worker.deliver(self.factory, self.run_id)
        journal.publish(self.factory, self.run_id)

        with self.factory() as db:
            run = db.get(Run, self.run_id)
            delivered = db.get(Outbox, (self.run_id, 0))
            self.assertEqual((run.completed_steps, run.published_steps), (1, 1))
            self.assertIsNotNone(delivered.published_at)
            self.assertIsNone(delivered.payload)
            self.assertEqual(journal.read_events(self.run_id, run.published_steps, after=-1, limit=2), [self.result])
            self.assertEqual(db.scalar(select(func.count()).select_from(Trade)), 1)
        self.assertEqual(event_file.stat().st_mtime_ns, written_at)
        self.assertEqual(list(event_file.parent.iterdir()), [event_file])


if __name__ == "__main__":
    unittest.main()
