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
                  limit_chunks: int = 0) -> dict:
    """limit_chunks: process only first N chunks per doc (frugal testing)."""
    con = S.connect()
    stats = {"docs": 0, "pages": 0, "chunks": 0, "facts": 0,
             "relations": 0, "questions": 0}
    for vi, path in enumerate(paths):
        doc = os.path.basename(path)
        sha = sha_of(path)
        row = con.execute("SELECT sha FROM documents WHERE doc=?", (doc,)).fetchone()
        if row and row[0] == sha and target_pages is None:
            print(f"[pipe] skip {doc} (unchanged)")
            continue
        pages = parse_pdf(path, target_pages)
        chunks = chunk_pages(pages, doc)
        if limit_chunks:
            chunks = chunks[:limit_chunks]
        entity = detect_doc_entity(chunks)
        S.upsert_document(con, doc, sha, len(pages), entity, vintage_base + vi)
        fid = 0
        seen: set[tuple] = set()  # (attr, value_raw, page) dedupe: headings repeat table data
        seen2: set[tuple] = set()  # (attr, value_norm, unit, page): ₹X vs X double-match
        for c in chunks:
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
        con.commit()
        stats["docs"] += 1
        stats["pages"] += len(pages)
    stats.update(link_all(con))
    stats.update(make_crops(con, {os.path.basename(p): p for p in paths}))
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


def link_all(con) -> dict:
    con.execute("DELETE FROM relations")
    facts = _all_facts(con)
    disclosures = {d: v for d, v in con.execute("SELECT doc,vintage FROM documents")}
    n_rel, n_q = 0, 0
    # blocking: only same-topic cross-doc pairs reach the LLM
    for i in range(len(facts)):
        for j in range(i + 1, len(facts)):
            a, b = facts[i], facts[j]
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
                S.insert_relation(con, rel)
                n_rel += 1
    con.commit()
    return {"relations": n_rel, "link_questions": n_q}


def make_crops(con, path_by_doc: dict) -> dict:
    """Render page PNGs for facts lacking crops. path_by_doc: doc->pdf path (or None to skip)."""
    n = 0
    rows = con.execute("SELECT fact_id,doc,page_index,crop FROM facts").fetchall()
    for fid, doc, pidx, crop in rows:
        if crop:
            continue
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
