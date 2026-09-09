"""Corpus management: delete one document, clear all, guards while running."""
import os
import time

from fastapi.testclient import TestClient

from app import store as S
from app import jobs as J
from app.config import settings
from app.main import app
from app.models import Evidence, Fact, Relation


def _ev(doc, crop=""):
    return Evidence(doc=doc, page_index=0, page_label="1", quote="q", crop=crop)


def _fact(fid, doc, crop=""):
    return Fact(fact_id=fid, subject="S", attribute="a", value_raw="1",
                evidence=_ev(doc, crop))


def _seed(tmp):
    con = S.connect()
    S.upsert_document(con, "a.pdf", "sha-a", 2, "Ent A", 0)
    S.upsert_document(con, "b.pdf", "sha-b", 3, "Ent B", 0)
    S.insert_fact(con, _fact("fa1", "a.pdf", "crops/a-p0.png"))
    S.insert_fact(con, _fact("fa2", "a.pdf"))
    S.insert_fact(con, _fact("fb1", "b.pdf"))
    S.insert_relation(con, Relation(relation="corroborates", fact_ids=["fa1", "fb1"],
                                    explanation="e", confidence=0.9, verified=True))
    S.insert_relation(con, Relation(relation="reconciled", fact_ids=["fa1", "fa2"],
                                    explanation="e2", confidence=0.5))
    S.insert_question(con, "ambiguous-period", "q1", ["fa2"], [_ev("a.pdf")])
    S.insert_question(con, "ambiguous-period", "q2", ["fb1"], [_ev("b.pdf")])
    con.commit()
    con.close()
    # files the deleter should remove
    os.makedirs(os.path.join(tmp, "uploads"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "crops"), exist_ok=True)
    open(os.path.join(tmp, "uploads", "a.pdf"), "w").write("x")
    open(os.path.join(tmp, "crops", "a-p0.png"), "w").write("x")
    open(os.path.join(tmp, "uploads", "b.pdf"), "w").write("x")


def _counts():
    con = S.connect()
    out = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for t in ("documents", "facts", "relations", "open_questions")}
    con.close()
    return out


def test_delete_one_document(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _seed(str(tmp_path))
    c = TestClient(app)
    r = c.delete("/api/documents/a.pdf")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d == {"docs": 1, "facts": 2, "relations": 2, "questions": 1,
                 "files_removed": ["a.pdf", "a-p0.png"]}
    left = _counts()
    assert left == {"documents": 1, "facts": 1, "relations": 0,
                    "open_questions": 1}  # only b.pdf's rows survive
    assert not os.path.exists(os.path.join(str(tmp_path), "uploads", "a.pdf"))
    assert os.path.exists(os.path.join(str(tmp_path), "uploads", "b.pdf"))


def test_delete_unknown_is_404(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    S.connect().close()
    c = TestClient(app)
    r = c.delete("/api/documents/nope.pdf")
    assert r.status_code == 404


def test_delete_refused_while_job_running(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    S.connect().close()

    def slow(paths, progress, cancel):
        for _ in range(100):
            if cancel.is_set():
                from app.jobs import Cancelled
                raise Cancelled()
            time.sleep(0.05)
        return {}

    job = J.start_job(["x.pdf"], run_fn=slow)
    try:
        c = TestClient(app)
        assert c.delete("/api/documents/a.pdf").status_code == 409
        assert c.delete("/api/documents").status_code == 409
    finally:
        J.cancel_job(job["id"])


def test_clear_all(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _seed(str(tmp_path))
    c = TestClient(app)
    r = c.delete("/api/documents")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["cleared"] is True and d["docs"] == 2 and d["facts"] == 3
    assert _counts() == {"documents": 0, "facts": 0, "relations": 0,
                         "open_questions": 0}
    assert os.listdir(os.path.join(str(tmp_path), "uploads")) == []
    assert os.listdir(os.path.join(str(tmp_path), "crops")) == []
