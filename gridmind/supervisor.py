import os
import subprocess
import sys
from threading import Lock

from sqlalchemy import select

from .db import ROOT, Run
from .journal import event_directory
from .worker import ACTIVE, finish, run_lock


class Supervisor:
    def __init__(self, factory, url):
        self.factory = factory
        self.url = url
        self.children = {}
        self.guard = Lock()

    def start(self, run_id):
        logs = event_directory().parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with self.guard, (logs / f"{run_id}.log").open("ab") as output:
            self.children[run_id] = subprocess.Popen(
                [sys.executable, "-m", "gridmind.worker", run_id],
                cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, creationflags=flags,
                env={**os.environ, "GRIDMIND_DATABASE_URL": self.url},
            )

    def reap(self):
        with self.guard:
            dead = [key for key, process in self.children.items() if process.poll() is not None]
            for run_id in dead:
                self.children.pop(run_id)
                with self.factory.begin() as db:
                    run = run_lock(db, run_id)
                    if run and run.status in ACTIVE:
                        finish(db, run, "failed", "The locality worker exited unexpectedly. Check its local log.")

    def recover(self):
        with self.factory.begin() as db:
            for run in db.scalars(select(Run).where(Run.status.in_(ACTIVE)).with_for_update()).all():
                finish(db, run, "interrupted", "The application restarted during this run.")

    def close(self):
        with self.guard:
            for run_id, process in self.children.items():
                if process.poll() is not None:
                    continue
                with self.factory.begin() as db:
                    run = run_lock(db, run_id)
                    if run and run.status in ACTIVE:
                        run.status = "stopping"
            for process in self.children.values():
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=3)
        self.reap()
