"""Background processing jobs.

Uploads run in a worker thread (requests return instantly) while progress is
polled by the UI. Job state persists to ``data/jobs.json`` so a page refresh
or reopen reattaches to the running job instead of restarting it.

Cancel is cooperative: the pipeline checks the job's event between units of
work and stops, keeping whatever grounded facts were already extracted.
"""
import json
import os
import threading
import time
import uuid

from .config import settings

STAGES = ["queued", "parse", "extract", "link", "crops"]
# share of the overall bar per stage (extract dominates: LLM calls per chunk)
WEIGHTS = {"parse": 0.20, "extract": 0.60, "link": 0.15, "crops": 0.05}

ACTIVE = ("queued", "running")


class Cancelled(Exception):
    """Raised inside the pipeline when the user cancels the job."""


_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_cancel_events: dict[str, threading.Event] = {}


def _path() -> str:
    os.makedirs(settings.data_dir, exist_ok=True)
    return os.path.join(settings.data_dir, "jobs.json")


def _load() -> dict:
    try:
        with open(_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(jobs: dict) -> None:
    tmp = _path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(jobs, f)
    os.replace(tmp, _path())


def list_jobs(limit: int = 5) -> list[dict]:
    jobs = _load()
    return sorted(jobs.values(), key=lambda j: j.get("started_at", 0),
                  reverse=True)[:limit]


def get_job(jid: str) -> dict | None:
    return _load().get(jid)


def active_job() -> dict | None:
    for j in list_jobs(limit=10):
        if j.get("status") in ACTIVE:
            # a "running" job with no live thread means the server restarted
            if j["status"] == "running" and j["id"] not in _threads:
                continue
            return j
    return None


def reconcile() -> None:
    """Mark jobs left 'running' by a server restart as interrupted."""
    with _lock:
        jobs = _load()
        changed = False
        for j in jobs.values():
            if j.get("status") in ACTIVE and j["id"] not in _threads:
                j["status"] = "interrupted"
                j["error"] = ("server restarted mid-run — already-parsed pages "
                              "are cached, just re-upload to resume cheaply")
                j["updated_at"] = time.time()
                changed = True
        if changed:
            _save(jobs)


def _update(jid: str, **fields) -> dict:
    with _lock:
        jobs = _load()
        job = jobs.get(jid, {})
        job.update(fields)
        job["updated_at"] = time.time()
        jobs[jid] = job
        _save(jobs)
        return job


def _fraction(stage: str, done: int, total: int) -> float:
    if stage not in WEIGHTS or total <= 0:
        return 0.0
    base = sum(WEIGHTS[s] for s in ("parse", "extract", "link", "crops")
               if STAGES.index(s) < STAGES.index(stage))
    return min(0.999, base + WEIGHTS[stage] * min(done / total, 1.0))


def _reporter(jid: str, started: float):
    def report(stage: str, done: int, total: int, detail: str = "") -> None:
        frac = _fraction(stage, done, total)
        elapsed = time.time() - started
        eta = (elapsed * (1 - frac) / frac) if frac > 0.03 else None
        _update(jid, status="running", stage=stage, stage_done=done,
                stage_total=total, detail=detail, frac=round(frac, 4),
                elapsed_s=int(elapsed),
                eta_s=int(eta) if eta is not None else None)
    return report


def start_job(paths: list[str], run_fn=None) -> dict:
    """Save-file upload calls this; returns the job (or the blocking active one)."""
    if active_job():
        return {"error": "a job is already running — cancel it first",
                "active": active_job()}
    jid = uuid.uuid4().hex[:8]
    now = time.time()
    job = {"id": jid, "files": [os.path.basename(p) for p in paths],
           "status": "queued", "stage": "queued", "stage_done": 0,
           "stage_total": 0, "detail": "waiting to start…", "frac": 0.0,
           "elapsed_s": 0, "eta_s": None, "stats": {}, "error": None,
           "started_at": now, "updated_at": now}
    with _lock:
        jobs = _load()
        jobs[jid] = job
        _save(jobs)
    ev = threading.Event()
    _cancel_events[jid] = ev
    t = threading.Thread(target=_run, args=(jid, paths, ev, run_fn),
                         daemon=True, name=f"job-{jid}")
    _threads[jid] = t
    t.start()
    return get_job(jid)


def _run(jid: str, paths: list[str], ev: threading.Event, run_fn) -> None:
    from .pipeline import process_files
    started = time.time()
    _update(jid, status="running", stage="parse", detail="starting…")
    try:
        fn = run_fn or process_files
        stats = fn(paths, progress=_reporter(jid, started), cancel=ev)
        _update(jid, status="done", stage="done", frac=1.0,
                detail="finished", stats=stats or {},
                elapsed_s=int(time.time() - started), eta_s=0)
    except Cancelled:
        partial = _last_stats(jid)
        _update(jid, status="cancelled", detail="cancelled by user",
                stats=partial, elapsed_s=int(time.time() - started),
                eta_s=None)
    except Exception as e:  # never leave the UI polling forever
        _update(jid, status="error", detail="failed",
                error=f"{type(e).__name__}: {e}",
                elapsed_s=int(time.time() - started), eta_s=None)
    finally:
        _threads.pop(jid, None)
        _cancel_events.pop(jid, None)


def _last_stats(jid: str) -> dict:
    # best-effort partial counts from the DB for the cancelled-job message
    try:
        from . import store as S
        con = S.connect()
        n_facts = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        n_docs = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        con.close()
        return {"facts_so_far": n_facts, "docs_so_far": n_docs,
                "note": "partial results kept — grounded facts stay usable"}
    except Exception:
        return {}


def cancel_job(jid: str) -> dict | None:
    job = get_job(jid)
    if not job or job.get("status") not in ACTIVE:
        return job
    ev = _cancel_events.get(jid)
    if ev is not None:
        ev.set()  # cooperative: pipeline stops at the next chunk boundary
        _update(jid, detail="cancelling… (finishes current step)")
    else:  # server restarted since: nothing to signal, just close it out
        _update(jid, status="cancelled", detail="cancelled",
                error="job was already dead after restart")
    return get_job(jid)
