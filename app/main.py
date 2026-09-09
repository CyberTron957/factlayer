"""FastAPI: upload PDFs, inspect facts/relations/timeline/open-questions/cases."""
import os
import sqlite3

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import store as S
from .config import settings
from . import jobs as J
from .pipeline import _all_facts

app = FastAPI(title="Fact Knowledge Layer")


@app.on_event("startup")
def _reconcile_jobs():
    J.reconcile()  # jobs left running by a restart become "interrupted"


def _rel_rows(con):
    cols = ["id", "relation", "axis", "fact_a", "fact_b",
            "explanation", "confidence", "verified"]
    return [dict(zip(cols, r)) for r in
            con.execute("SELECT * FROM relations ORDER BY confidence DESC")]


def _fact_map(con):
    return {f.fact_id: f for f in _all_facts(con)}


@app.get("/api/documents")
def documents():
    con = S.connect()
    rows = [dict(zip(["doc", "sha", "pages", "entity", "vintage"], r))
            for r in con.execute("SELECT * FROM documents")]
    con.close()
    return rows


@app.get("/api/facts")
def facts(q: str = "", doc: str = "", limit: int = 500):
    con = S.connect()
    fmap = _fact_map(con)
    out = []
    for f in fmap.values():
        d = f.model_dump()
        blob = (f.subject + f.attribute + f.value_raw + f.period).lower()
        if q and q.lower() not in blob:
            continue
        if doc and f.evidence.doc != doc:
            continue
        out.append(d)
    con.close()
    return out[:limit]


@app.get("/api/relations")
def relations(reltype: str = ""):
    con = S.connect()
    fmap = _fact_map(con)
    out = []
    for r in _rel_rows(con):
        if reltype and r["relation"] != reltype:
            continue
        r["facts"] = [fmap.get(r["fact_a"], {}), fmap.get(r["fact_b"], {})]
        r["facts"] = [f.model_dump() if hasattr(f, "model_dump") else f for f in r["facts"]]
        out.append(r)
    con.close()
    return out


@app.get("/api/questions")
def questions():
    import json
    con = S.connect()
    rows = [dict(zip(["id", "kind", "detail", "fact_ids", "evidence"], r))
            for r in con.execute("SELECT * FROM open_questions ORDER BY id LIMIT 300")]
    for r in rows:
        r["fact_ids"] = json.loads(r["fact_ids"] or "[]")
        r["evidence"] = json.loads(r["evidence"] or "[]")
    con.close()
    return rows


@app.get("/api/timeline")
def timeline():
    con = S.connect()
    fmap = _fact_map(con)
    facts = sorted(
        (f for f in fmap.values() if f.period),
        key=lambda f: (f.period, f.evidence.doc))
    sup = [r for r in _rel_rows(con) if r["relation"] == "superseded-by"]
    con.close()
    return {"facts": [f.model_dump() for f in facts[:1000]], "supersessions": sup}


@app.get("/api/cases")
def cases():
    """The four required demo cases, each with evidence + reasoning."""
    con = S.connect()
    fmap = _fact_map(con)
    rels = _rel_rows(con)
    import json

    def with_facts(r):
        r = dict(r)
        fs = []
        for fid in (r["fact_a"], r["fact_b"]):
            f = fmap.get(fid)
            fs.append(f.model_dump() if f else {"fact_id": fid})
        r["facts"] = fs
        return r

    def quality(r):
        # Rank showcase pairs by link-quality signals: same dimension and
        # period, overlapping attribute tokens, clean non-chart evidence.
        fs = [fmap.get(fid) for fid in (r["fact_a"], r["fact_b"])]
        if any(f is None for f in fs):
            return -1.0
        a, b = fs
        try:
            from .normalize import same_dimension
            if not same_dimension(a.unit_norm or "", b.unit_norm or ""):
                return -1.0  # cross-dimension pairs never showcase
        except Exception:
            pass
        score = 0.0
        if (a.unit_norm or "") == (b.unit_norm or "") and a.unit_norm:
            score += 2.0
        if (a.period or "") == (b.period or "") and a.period:
            score += 1.0
        ta = set((a.attribute or "").lower().split())
        tb = set((b.attribute or "").lower().split())
        if ta and tb:
            score += len(ta & tb) / max(len(ta | tb), 1)
        score += 0.1 * min(a.confidence or 0, b.confidence or 0)
        for f in fs:
            if (f.evidence.modality or "") in ("chart", "infographic"):
                score -= 0.15
            if len((f.attribute or "").split()) > 6:
                score -= 0.1
        return score + (0.05 if r.get("verified") else 0)

    def pick(rel):
        cands = [r for r in rels if r["relation"] == rel]
        if not cands:
            return None
        cands.sort(key=quality, reverse=True)
        return with_facts(cands[0])

    qs = con.execute("SELECT kind,detail,fact_ids,evidence FROM open_questions ORDER BY id").fetchall()
    failure = None
    # failure priority: hard errors first, then provisional chart facts
    # (a genuine reasoning limitation), then ambiguity notes
    prio = {"verifier-rejection": 0, "link-error": 1, "llm-extract-error": 1,
            "chart-sourced-provisional": 2, "ambiguous-period": 3}
    for kind, detail, fids, ev in sorted(qs, key=lambda r: prio.get(r[0], 9)):
        failure = {"kind": kind, "detail": detail,
                   "fact_ids": json.loads(fids or "[]"),
                   "evidence": json.loads(ev or "[]")}
        break
    con.close()
    return {"corroborated": pick("corroborates"),
            "contradiction": pick("contradicts"),
            "reconciled": pick("reconciled"),
            "failure": failure}


@app.get("/api/export")
def export():
    import csv
    import io
    con = S.connect()
    facts = _all_facts(con)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["fact_id", "subject", "attribute", "value_raw", "unit",
                "period", "scope", "type", "confidence", "doc", "page", "quote"])
    for f in facts:
        w.writerow([f.fact_id, f.subject, f.attribute, f.value_raw,
                    f.unit_norm, f.period, f.scope, f.fact_type,
                    f.confidence, f.evidence.doc, f.evidence.page_label,
                    f.evidence.quote[:300]])
    rels = _rel_rows(con)
    con.close()
    return JSONResponse({"facts_csv": buf.getvalue(), "relations": rels})


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    updir = os.path.join(settings.data_dir, "uploads")
    os.makedirs(updir, exist_ok=True)
    paths = []
    for uf in files:
        if not uf.filename.lower().endswith(".pdf"):
            continue
        dest = os.path.join(updir, os.path.basename(uf.filename))
        with open(dest, "wb") as f:
            f.write(await uf.read())
        paths.append(dest)
    if not paths:
        return {"error": "no PDFs received"}
    # async: returns instantly with a job id; the UI polls /api/jobs/<id>
    job = J.start_job(paths)
    if job.get("error"):  # NB: job dicts always CONTAIN "error" (None on success)
        return JSONResponse(job, status_code=409)
    return {"job_id": job["id"], "files": job["files"]}


@app.delete("/api/documents/{doc}")
def delete_document(doc: str):
    """Delete one document and everything derived from it (facts, relations,
    questions, crops, uploaded PDF). Refused while a job is running."""
    if J.active_job():
        return JSONResponse(
            {"error": "a job is running — cancel it before deleting"},
            status_code=409)
    res = S.delete_document(doc)
    if not res["docs"]:
        return JSONResponse({"error": f"unknown document: {doc}"},
                            status_code=404)
    return res


@app.delete("/api/documents")
def clear_documents():
    """Wipe the whole corpus. Refused while a job is running."""
    if J.active_job():
        return JSONResponse(
            {"error": "a job is running — cancel it before clearing"},
            status_code=409)
    return S.clear_all()


@app.get("/api/jobs")
def jobs():
    return J.list_jobs()


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    job = J.get_job(jid)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return job


@app.post("/api/jobs/{jid}/cancel")
def job_cancel(jid: str):
    job = J.cancel_job(jid)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return job


@app.get("/api/crop")
def crop(path: str):
    full = os.path.abspath(path)
    base = os.path.abspath(settings.data_dir)
    if not full.startswith(base) or not os.path.exists(full):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(full, media_type="image/png")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
