"""SQLite knowledge layer. Tables: documents, facts, relations,
open_questions, schema_registry (dynamic attribute catalog)."""
import json
import os
import sqlite3

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  doc TEXT PRIMARY KEY, sha TEXT, pages INTEGER, entity TEXT, vintage INTEGER);
CREATE TABLE IF NOT EXISTS facts(
  fact_id TEXT PRIMARY KEY, doc TEXT, subject TEXT, attribute TEXT,
  value_raw TEXT, value_norm REAL, unit_norm TEXT, period TEXT, scope TEXT,
  fact_type TEXT, confidence REAL, page_index INTEGER, page_label TEXT,
  quote TEXT, modality TEXT, crop TEXT, flags TEXT);
CREATE TABLE IF NOT EXISTS relations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, relation TEXT, axis TEXT,
  fact_a TEXT, fact_b TEXT, explanation TEXT, confidence REAL, verified INTEGER);
CREATE TABLE IF NOT EXISTS open_questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, detail TEXT,
  fact_ids TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS schema_registry(
  attribute TEXT PRIMARY KEY, first_seen TEXT, count INTEGER);
"""


def db_path() -> str:
    os.makedirs(settings.data_dir, exist_ok=True)
    return os.path.join(settings.data_dir, "knowledge.db")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.executescript(SCHEMA)
    return con


def clear(con: sqlite3.Connection | None = None):
    con = con or connect()
    for t in ["relations", "facts", "open_questions", "documents", "schema_registry"]:
        con.execute(f"DELETE FROM {t}")
    con.commit()


def upsert_document(con, doc, sha, pages, entity, vintage):
    con.execute(
        "INSERT OR REPLACE INTO documents(doc,sha,pages,entity,vintage) VALUES(?,?,?,?,?)",
        (doc, sha, pages, entity, vintage))


def insert_fact(con, f) -> None:
    con.execute(
        """INSERT OR REPLACE INTO facts(fact_id,doc,subject,attribute,value_raw,
        value_norm,unit_norm,period,scope,fact_type,confidence,page_index,
        page_label,quote,modality,crop,flags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f.fact_id, f.evidence.doc, f.subject, f.attribute, f.value_raw,
         f.value_norm, f.unit_norm, f.period, f.scope, f.fact_type,
         f.confidence, f.evidence.page_index, f.evidence.page_label,
         f.evidence.quote, f.evidence.modality, f.evidence.crop or "",
         json.dumps(f.flags)))
    con.execute(
        """INSERT INTO schema_registry(attribute,first_seen,count) VALUES(?,?,1)
        ON CONFLICT(attribute) DO UPDATE SET count=count+1""",
        (f.attribute.lower().strip(), f.evidence.doc))


def insert_relation(con, r) -> None:
    a, b = (r.fact_ids + ["", ""])[:2]
    con.execute(
        """INSERT INTO relations(relation,axis,fact_a,fact_b,explanation,
        confidence,verified) VALUES(?,?,?,?,?,?,?)""",
        (r.relation, r.axis, a, b, r.explanation, r.confidence,
         1 if r.verified else 0))


def insert_question(con, kind, detail, fact_ids=None, evidence=None) -> None:
    con.execute(
        "INSERT INTO open_questions(kind,detail,fact_ids,evidence) VALUES(?,?,?,?)",
        (kind, detail, json.dumps(fact_ids or []),
         json.dumps([e.model_dump() for e in (evidence or [])])))


def _inside_data_dir(path: str) -> str | None:
    """Resolve *path* against the data dir; None if it would escape."""
    base = os.path.abspath(settings.data_dir)
    full = path if os.path.isabs(path) else os.path.join(base, path)
    full = os.path.abspath(full)
    return full if full.startswith(base + os.sep) else None


def delete_document(doc: str) -> dict:
    """Remove one document and everything derived from it.

    Deletes its facts, relations touching those facts, open questions that
    reference them, plus the uploaded PDF and rendered crops. Parse-cache
    entries are kept on purpose — re-uploading the same file stays cheap.
    Returns counts of what was removed.
    """
    con = connect()
    fids = [r[0] for r in con.execute(
        "SELECT fact_id FROM facts WHERE doc=?", (doc,))]
    crops = [r[0] for r in con.execute(
        "SELECT DISTINCT crop FROM facts WHERE doc=? AND crop<>''", (doc,))]
    n_rel = 0
    if fids:
        ph = ",".join("?" * len(fids))
        n_rel = con.execute(
            f"DELETE FROM relations WHERE fact_a IN ({ph}) OR fact_b IN ({ph})",
            fids + fids).rowcount or 0
    n_q = 0
    fidset = set(fids)
    for qid, qfids in con.execute("SELECT id, fact_ids FROM open_questions"):
        try:
            refs = json.loads(qfids or "[]")
        except ValueError:
            continue
        if any(i in fidset for i in refs):
            con.execute("DELETE FROM open_questions WHERE id=?", (qid,))
            n_q += 1
    n_facts = con.execute("DELETE FROM facts WHERE doc=?", (doc,)).rowcount or 0
    n_docs = con.execute("DELETE FROM documents WHERE doc=?", (doc,)).rowcount or 0
    con.commit()
    con.close()
    removed = []
    candidates = [os.path.join(settings.data_dir, "uploads", os.path.basename(doc))]
    for c in crops:
        full = _inside_data_dir(c)
        if full:
            candidates.append(full)
    for p in candidates:
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed.append(os.path.basename(p))
        except OSError:
            pass
    return {"docs": n_docs, "facts": n_facts, "relations": n_rel,
            "questions": n_q, "files_removed": removed}


def clear_all() -> dict:
    """Wipe every document, fact, relation, question, upload, and crop."""
    con = connect()
    n_docs = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    n_facts = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    clear(con)
    con.close()
    removed = 0
    for sub in ("uploads", "crops"):
        d = os.path.join(settings.data_dir, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
    return {"cleared": True, "docs": n_docs, "facts": n_facts,
            "files_removed": removed}
