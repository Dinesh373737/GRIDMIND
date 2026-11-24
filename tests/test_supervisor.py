from gridmind.supervisor import Supervisor


def test_simulation_worker_does_not_receive_groq_key(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-only-fixture")
    monkeypatch.setenv("GRIDMIND_EVENT_DIR", str(tmp_path / "events"))
    child = object()
    launches = []

    def popen(*args, **kwargs):
        launches.append((args, kwargs))
        return child

    monkeypatch.setattr("gridmind.supervisor.subprocess.Popen", popen)
    supervisor = Supervisor(None, "sqlite:///test.db")
    supervisor.start("fixture-run")
    assert supervisor.children["fixture-run"] is child
    env = launches[0][1]["env"]
    assert "GROQ_API_KEY" not in env
    assert env["GRIDMIND_EVENT_DIR"] == str(tmp_path / "events")
    assert env["GRIDMIND_DATABASE_URL"] == "sqlite:///test.db"
