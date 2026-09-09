"""Job lifecycle: instant done, cooperative cancel, stale-job reconcile, bar math."""
import time

from app import jobs as J
from app.config import settings
from app.jobs import Cancelled


def _wait(jid, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        j = J.get_job(jid)
        if j and j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {jid} never finished: {J.get_job(jid)}")


def test_start_to_done(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    seen = []

    def fake(paths, progress, cancel):
        progress("parse", 1, 1, "parsed")
        progress("extract", 2, 2, "extracted")
        seen.append(True)
        return {"facts": 3}

    job = J.start_job(["a.pdf"], run_fn=fake)
    assert job["status"] in ("queued", "running")
    final = _wait(job["id"])
    assert final["status"] == "done"
    assert final["frac"] == 1.0
    assert final["stats"] == {"facts": 3}
    assert seen  # runner actually ran the function


def test_cancel_stops_work(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    def slow(paths, progress, cancel):
        for i in range(1000):
            if cancel.is_set():
                raise Cancelled()
            progress("extract", i, 1000, f"chunk {i}")
            time.sleep(0.005)
        return {}

    job = J.start_job(["big.pdf"], run_fn=slow)
    time.sleep(0.3)  # let it reach running
    out = J.cancel_job(job["id"])
    assert out is not None
    final = _wait(job["id"])
    assert final["status"] == "cancelled"


def test_cancel_unknown_job(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    assert J.cancel_job("nope") is None


def test_reconcile_marks_stale_interrupted(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    import json
    stale = {"dead": {"id": "dead", "files": ["x.pdf"], "status": "running",
                      "stage": "extract", "started_at": 1, "updated_at": 1}}
    with open(tmp_path / "jobs.json", "w") as f:
        json.dump(stale, f)
    J.reconcile()
    assert J.get_job("dead")["status"] == "interrupted"


def test_fraction_weights():
    assert J._fraction("parse", 1, 2) == 0.1  # 20% * 1/2
    assert J._fraction("extract", 1, 2) == 0.5  # 20% + 60% * 1/2
    assert J._fraction("link", 0, 0) == 0.0
    assert 0.0 <= J._fraction("crops", 99, 100) <= 1.0


def test_start_result_error_key_contract(monkeypatch, tmp_path):
    # main.py branches on job.get("error"): success MUST be falsy, blocked truthy.
    # Regression: `"error" in job` is always True since the key exists (None).
    import threading
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    gate = threading.Event()

    def hanging(paths, progress, cancel):
        gate.wait(10)
        return {}

    ok = J.start_job(["a.pdf"], run_fn=hanging)
    assert not ok.get("error") and ok.get("id")
    blocked = J.start_job(["b.pdf"], run_fn=hanging)
    assert blocked.get("error") and "active" in blocked
    J.cancel_job(ok["id"])
    gate.set()
    _wait(ok["id"])


def test_second_start_blocked_while_running(monkeypatch, tmp_path):
    import threading
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    gate = threading.Event()

    def hanging(paths, progress, cancel):
        gate.wait(10)
        return {}

    first = J.start_job(["one.pdf"], run_fn=hanging)
    second = J.start_job(["two.pdf"], run_fn=hanging)
    assert "error" in second  # recruiter can't accidentally double-run
    J.cancel_job(first["id"])
    gate.set()
    _wait(first["id"])
