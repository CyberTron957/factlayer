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
