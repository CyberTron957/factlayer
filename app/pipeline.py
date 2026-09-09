"""Pipeline: PDFs -> grounded facts -> verified relations. Incremental:
documents already in storage (by SHA) are skipped; new docs link against all
stored facts without rebuilding anything."""
import json
import os

from .chunk import chunk_pages
from .config import settings
from .extract import detect_doc_entity, extract_chunk
from .link import link_pair, same_topic
from .models import Fact
from .parse import parse_pdf, render_crop, sha_of
from . import store as S
from .verify import verify_fact


def process_files(paths: list[str], vintage_base: int = 0,
                   target_pages: str | None = None,
                   limit_chunks: int = 0,
                   progress=None, cancel=None) -> dict:
    """limit_chunks: process only first N chunks per doc (frugal testing).

    progress: optional callable(stage, done, total, detail) for the UI bar.
    cancel: optional threading.Event; Cancelled is raised at chunk boundaries.
    Cancel-safe: doc work commits per doc; link_all only rewrites relations
    once the full pass succeeds, so a cancelled job keeps partial facts and
    leaves old relations intact.
    """
    from .jobs import Cancelled

    def rep(stage, done, total, detail=""):
        if progress:
            progress(stage, done, total, detail)

    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    con = S.connect()
    stats = {"docs": 0, "pages": 0, "chunks": 0, "facts": 0,
             "relations": 0, "questions": 0}
    try:
        # phase 1 — parse (slow for new docs, instant from cache)
        parsed = []  # (vintage_idx, doc, path, sha, pages)
        for vi, path in enumerate(paths):
            check()
            doc = os.path.basename(path)
            sha = sha_of(path)
            row = con.execute("SELECT sha FROM documents WHERE doc=?", (doc,)).fetchone()
            if row and row[0] == sha and target_pages is None:
                print(f"[pipe] skip {doc} (unchanged)")
                continue
            rep("parse", vi, len(paths), f"parsing {doc}…")
            pages = parse_pdf(path, target_pages)
            parsed.append((vi, doc, path, sha, pages))
            rep("parse", vi + 1, len(paths), f"{doc}: {len(pages)} pages")
        # phase 2 — chunk + entity (fast, local)
        chunked = []  # (vi, doc, path, pages, chunks, entity)
        for vi, doc, path, sha, pages in parsed:
            check()
            chunks = chunk_pages(pages, doc)
            if limit_chunks:
                chunks = chunks[:limit_chunks]
            entity = detect_doc_entity(chunks)
            S.upsert_document(con, doc, sha, len(pages), entity, vintage_base + vi)
            chunked.append((vi, doc, path, pages, chunks, entity))
        total_chunks = sum(len(c[4]) for c in chunked) or 1
        # phase 3 — extract (dominates runtime: LLM calls per chunk)
        done = 0
        for vi, doc, path, pages, chunks, entity in chunked:
            fid = 0
            seen: set[tuple] = set()  # (attr, value_raw, page): headings repeat table data
            seen2: set[tuple] = set()  # (attr, value_norm, unit, page): ₹X vs X double-match
            for k, c in enumerate(chunks):
                check()
                facts, questions = extract_chunk(c, entity)
                stats["chunks"] += 1
                for q in questions:
                    S.insert_question(con, q.get("kind", "extract-note"),
                                      q.get("detail", ""), [], [])
                    stats["questions"] += 1
                for f in facts:
                    ok, reason = verify_fact(f, c.text)
                    if not ok:
                        S.insert_question(con, "verifier-rejection", reason,
                                          [], [f.evidence])
                        stats["questions"] += 1
                        continue
                    key = (f.attribute.lower().strip(), f.value_raw.strip(), f.evidence.page_index)
                    key2 = (f.attribute.lower().strip(), f.value_norm, f.unit_norm, f.evidence.page_index)
                    if key in seen or key2 in seen2:
                        continue
                    seen.add(key)
                    seen2.add(key2)
                    fid += 1
                    f.fact_id = f"{doc[:6]}-{f.evidence.page_index:03d}-{fid:04d}"
                    if "chart-sourced" in f.flags or f.evidence.modality in ("chart", "infographic"):
                        S.insert_question(con, "chart-sourced-provisional",
                                          f"{f.attribute} = {f.value_raw} awaits table/text corroboration.",
                                          [f.fact_id], [f.evidence])
                        stats["questions"] += 1
                    if "no-period" in f.flags and f.fact_type == "numeric":
                        S.insert_question(con, "ambiguous-period",
                                          f"{f.attribute} = {f.value_raw} has no detectable period.",
                                          [f.fact_id], [f.evidence])
                        stats["questions"] += 1
                    S.insert_fact(con, f)
                    stats["facts"] += 1
                done += 1
                rep("extract", done, total_chunks,
                    f"{doc} — chunk {k + 1}/{len(chunks)} · {stats['facts']} facts")
            con.commit()
            stats["docs"] += 1
            stats["pages"] += len(pages)
        stats.update(link_all(con, progress=rep, cancel=cancel))
        stats.update(make_crops(con, {os.path.basename(p): p for p in paths},
                                progress=rep, cancel=cancel))
    except Exception:
        try:
            con.commit()  # keep partial grounded facts on cancel/failure
        except Exception:
            pass
        con.close()
        raise
    con.close()
    return stats


def _all_facts(con) -> list[Fact]:
    from .models import Evidence
    out = []
    for r in con.execute("SELECT * FROM facts"):
        d = dict(zip([c[0] for c in con.execute("SELECT * FROM facts LIMIT 0").description], r))
        out.append(Fact(
            fact_id=d["fact_id"], subject=d["subject"], attribute=d["attribute"],
            value_raw=d["value_raw"], value_norm=d["value_norm"],
            unit_norm=d["unit_norm"], period=d["period"], scope=d["scope"],
            fact_type=d["fact_type"], confidence=d["confidence"],
            evidence=Evidence(doc=d["doc"], page_index=d["page_index"],
                              page_label=d["page_label"], quote=d["quote"],
                              crop=d["crop"] or None, modality=d["modality"] or "unknown"),
            flags=json.loads(d["flags"] or "[]")))
    return out


def link_all(con, progress=None, cancel=None) -> dict:
    from .jobs import Cancelled
    facts = _all_facts(con)
    disclosures = {d: v for d, v in con.execute("SELECT doc,vintage FROM documents")}
    total = len(facts) * (len(facts) - 1) // 2 or 1
    rels, n_q, examined = [], 0, 0
    # blocking: only same-topic cross-doc pairs reach the LLM
    for i in range(len(facts)):
        for j in range(i + 1, len(facts)):
            a, b = facts[i], facts[j]
            examined += 1
            if examined % 200 == 0:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if progress:
                    progress("link", examined, total,
                             f"comparing pairs {examined}/{total} · {len(rels)} linked")
            if not same_topic(a, b):
                continue
            try:
                rel = link_pair(a, b, settings.link_tolerance, disclosures)
            except Exception as e:
                S.insert_question(con, "link-error", f"{type(e).__name__}: {e}",
                                  [a.fact_id, b.fact_id], [a.evidence, b.evidence])
                n_q += 1
                continue
            if rel:
                rels.append(rel)
    # atomic rewrite only after the full pass: a cancelled job keeps old relations
    con.execute("DELETE FROM relations")
    for rel in rels:
        S.insert_relation(con, rel)
    con.commit()
    if progress:
        progress("link", total, total, f"{len(rels)} relations")
    return {"relations": len(rels), "link_questions": n_q}


def make_crops(con, path_by_doc: dict, progress=None, cancel=None) -> dict:
    """Render page PNGs for facts lacking crops. path_by_doc: doc->pdf path (or None to skip)."""
    from .jobs import Cancelled
    n = 0
    rows = con.execute("SELECT fact_id,doc,page_index,crop FROM facts").fetchall()
    missing = [(fid, doc, pidx) for fid, doc, pidx, crop in rows if not crop]
    total = len(missing) or 1
    for k, (fid, doc, pidx) in enumerate(missing):
        if cancel is not None and cancel.is_set():
            con.commit()
            raise Cancelled()
        pdf = path_by_doc.get(doc)
        if not pdf or not os.path.exists(pdf):
            continue
        out = os.path.join(settings.data_dir, "crops", f"{doc[:8]}-p{pidx}.png")
        try:
            render_crop(pdf, pidx, out)
            con.execute("UPDATE facts SET crop=? WHERE fact_id=?", (out, fid))
            n += 1
        except Exception:
            pass
        if progress and (k % 5 == 0 or k == len(missing) - 1):
            progress("crops", k + 1, total, f"page snapshots {k + 1}/{len(missing)}")
    con.commit()
    # backfill crops into open-question evidence by matching fact_ids
    import json as _json
    for qid, fids in con.execute("SELECT id,fact_ids FROM open_questions"):
        try:
            ids = _json.loads(fids or "[]")
        except Exception:
            continue
        if not ids:
            continue
        row = con.execute(
            "SELECT crop FROM facts WHERE fact_id IN (%s) AND crop<>'' LIMIT 1"
            % ",".join("?" * len(ids)), ids).fetchone()
        if row:
            ev_rows = con.execute("SELECT evidence FROM open_questions WHERE id=?", (qid,)).fetchone()
            try:
                evs = _json.loads(ev_rows[0] or "[]")
                for e in evs:
                    e["crop"] = e.get("crop") or row[0]
                con.execute("UPDATE open_questions SET evidence=? WHERE id=?",
                            (_json.dumps(evs), qid))
            except Exception:
                pass
    con.commit()
    return {"crops": n}
