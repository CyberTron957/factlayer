"""Pipeline: PDFs -> grounded facts -> verified relations. Incremental:
documents already in storage (by SHA) are skipped; new docs link against all
stored facts without rebuilding anything."""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .chunk import chunk_pages
from .config import settings
from .extract import detect_doc_entity, extract_chunk
from .link import (LinkBudget, _candidate_score, _finalize, _pair_plan,
                   heuristic_link, judge_pair, same_topic)
from .models import Fact
from .parse import parse_pdf, render_crop, sha_of
from . import store as S
from .verify import verify_fact


_YEAR_RE = re.compile(r"(?<![\d.,])(19\d{2}|20[0-3]\d)(?!\d)")


def _doc_vintage(doc: str, pages, fallback: int = 0) -> int:
    """Disclosure vintage from CONTENT (filing years), never upload order.

    Same-period conflicts between sibling disclosures (e.g. earnings release
    vs annual report, both FY2025) must be contradictions — upload position
    in a batch must not decide which fact "wins". Only a genuinely newer
    disclosure (restatement with later filing years) supersedes.
    Returns max content year in [1990, 2030], else filename year, else fallback.
    """
    years = [int(y) for p in pages
             for y in _YEAR_RE.findall(getattr(p, "markdown", "") or "")]
    years = [y for y in years if 1990 <= y <= 2030]
    if years:
        return max(years)
    m = _YEAR_RE.search(doc or "")
    if m and 1990 <= int(m.group(1)) <= 2030:
        return int(m.group(1))
    return fallback


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
        # phase 1 — parse (slow for new docs, instant from cache).
        # Unchanged docs are skipped serially (cheap DB check); new docs fan
        # out across threads — each parse_pdf call is independent (own cache
        # file) and mostly waits on the cloud API.
        todo = []  # (vintage_idx, doc, path, sha)
        for vi, path in enumerate(paths):
            check()
            doc = os.path.basename(path)
            sha = sha_of(path)
            row = con.execute("SELECT sha FROM documents WHERE doc=?", (doc,)).fetchone()
            if row and row[0] == sha and target_pages is None:
                print(f"[pipe] skip {doc} (unchanged)")
                continue
            todo.append((vi, doc, path, sha))
        parsed = []  # (vintage_idx, doc, path, sha, pages)
        if len(todo) > 1:
            n_w = min(settings.parse_workers, len(todo))
            ex = ThreadPoolExecutor(max_workers=n_w, thread_name_prefix="parse")
            futs = {ex.submit(parse_pdf, path, target_pages): (vi, doc, path, sha)
                    for (vi, doc, path, sha) in todo}
            try:
                done = 0
                for fut in as_completed(futs):
                    vi, doc, path, sha = futs[fut]
                    pages = fut.result()
                    parsed.append((vi, doc, path, sha, pages))
                    done += 1
                    rep("parse", done, len(paths), f"{doc}: {len(pages)} pages")
                    check()
            except BaseException:
                for f in futs:
                    f.cancel()
                ex.shutdown(wait=True)
                raise
            ex.shutdown(wait=True)
            parsed.sort(key=lambda t: t[0])  # restore upload order (vintage fallback)
        else:
            for vi, doc, path, sha in todo:
                check()
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
            vintage = _doc_vintage(doc, pages, fallback=vintage_base + vi)
            S.upsert_document(con, doc, sha, len(pages), entity, vintage)
            chunked.append((vi, doc, path, pages, chunks, entity))
        total_chunks = sum(len(c[4]) for c in chunked) or 1
        # phase 3a — extract fan-out (LLM-bound: threads wait on network).
        # Workers are pure (no DB access); results land in chunk order so the
        # single-writer commit below keeps fact_ids, dedupe and per-doc
        # commits exactly as in the serial version. Cache keys make repeat
        # extractions of identical chunks (key rotation, DB wipe + re-upload)
        # free.
        from .extract_cache import make_key as _cache_key
        sha_by_doc = {doc: sha for (_, doc, _, sha, _) in parsed}
        items = [(c, entity, _cache_key(sha_by_doc.get(doc, ""), doc, entity,
                                        c.page_index, c.text))
                 for (_, doc, _, _, chunks, entity) in chunked for c in chunks]
        results: list = [None] * len(items)
        if len(items) > 1:
            n_w = min(settings.extract_workers, len(items))
            ex = ThreadPoolExecutor(max_workers=n_w, thread_name_prefix="extract")
            futs = {ex.submit(extract_chunk, c, e, k): n
                    for n, (c, e, k) in enumerate(items)}
            try:
                done = 0
                for fut in as_completed(futs):
                    results[futs[fut]] = fut.result()
                    done += 1
                    rep("extract", done, total_chunks,
                        f"extracted chunk {done}/{total_chunks}…")
                    check()
            except BaseException:
                for f in futs:
                    f.cancel()  # best-effort: unstarted work never runs
                ex.shutdown(wait=True)  # in-flight calls land (bounded by timeouts)
                raise
            ex.shutdown(wait=True)
        else:
            for n, (c, e, k) in enumerate(items):
                check()
                results[n] = extract_chunk(c, e, k)
                rep("extract", n + 1, total_chunks,
                    f"extracted chunk {n + 1}/{total_chunks}…")
        # phase 3b — verify + dedupe + commit (single writer, unchanged logic)
        done = 0
        pos = 0
        for vi, doc, path, pages, chunks, entity in chunked:
            fid = 0
            seen: set[tuple] = set()  # (attr, value_raw, page): headings repeat table data
            seen2: set[tuple] = set()  # (attr, value_norm, unit, page): ₹X vs X double-match
            for k, c in enumerate(chunks):
                check()
                facts, questions = results[pos]
                pos += 1
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
                    # full sanitized basename: doc[:6] prefixes collide
                    # ("report-Q1" vs "report-Q2") and INSERT OR REPLACE
                    # would silently annihilate the earlier doc's facts.
                    stem = re.sub(r"\W+", "_", doc)
                    f.fact_id = (f"{stem}-{f.evidence.page_index:03d}-{fid:04d}")
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


def link_all(con, progress=None, cancel=None, workers: int = 0) -> dict:
    from .jobs import Cancelled
    facts = _all_facts(con)
    disclosures = {d: v for d, v in con.execute("SELECT doc,vintage FROM documents")}
    tol = settings.link_tolerance
    rels, n_q, examined = [], 0, 0
    # pass A — blocking + heuristic (cheap, local, fully deterministic).
    # _pair_plan emits exactly the pairs a full scan could link — nothing
    # more — so verdicts match the unblocked version pair for pair.
    # Ambiguous pairs are collected with a rank score instead of burning the
    # judge budget in insertion order.
    total, pair_iter = _pair_plan(facts)
    cands = []  # (score, i, j)
    for i, j in pair_iter:
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
            rel = heuristic_link(a, b, tol)
        except Exception as e:
            S.insert_question(con, "link-error", f"{type(e).__name__}: {e}",
                              [a.fact_id, b.fact_id], [a.evidence, b.evidence])
            n_q += 1
            continue
        if rel.get("relation") in ("corroborates", "contradicts",
                                   "reconciled", "superseded-by"):
            r = _finalize(rel, a, b, tol, disclosures)
            if r:
                rels.append(r)
        else:
            cands.append((_candidate_score(a, b, tol), i, j))
    # pass B — LLM judge over top-ranked ambiguous pairs, fresh budget every
    # run. judge_pair is pure (no DB), so calls fan out; results assemble in
    # rank order, keeping verdicts deterministic.
    budget = LinkBudget()
    cands.sort(key=lambda t: t[0], reverse=True)
    judging = cands[:budget.remaining]
    verdicts: dict[int, dict] = {}
    if len(judging) > 1:
        n_w = workers or settings.link_workers
        n_w = min(n_w, len(judging))
        ex = ThreadPoolExecutor(max_workers=n_w, thread_name_prefix="linkjudge")
        futs = {ex.submit(judge_pair, facts[i], facts[j]): k
                for k, (_, i, j) in enumerate(judging)}
        try:
            done = 0
            for fut in as_completed(futs):
                verdicts[futs[fut]] = fut.result()
                done += 1
                if progress and (done % 10 == 0 or done == len(judging)):
                    progress("link", total + done, total + len(judging),
                             f"judging ambiguous pairs {done}/{len(judging)} · {len(rels)} linked")
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
        except BaseException:
            for f in futs:
                f.cancel()
            ex.shutdown(wait=True)
            raise
        ex.shutdown(wait=True)
    else:
        for k, (_, i, j) in enumerate(judging):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            verdicts[k] = judge_pair(facts[i], facts[j])
    for k in sorted(verdicts):
        _, i, j = judging[k]
        rel = verdicts[k]
        if rel:
            r = _finalize(rel, facts[i], facts[j], tol, disclosures)
            if r:
                rels.append(r)
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
    # render once per (doc, page): many facts share a page and the output
    # filename is per-page, so per-fact rendering just rewrote the same PNG.
    by_page: dict[tuple, list] = {}
    for fid, doc, pidx in missing:
        by_page.setdefault((doc, pidx), []).append(fid)
    pages = list(by_page)
    total = len(pages) or 1
    for k, (doc, pidx) in enumerate(pages):
        if cancel is not None and cancel.is_set():
            con.commit()
            raise Cancelled()
        pdf = path_by_doc.get(doc)
        if not pdf or not os.path.exists(pdf):
            continue
        out = os.path.join(settings.data_dir, "crops", f"{doc[:8]}-p{pidx}.png")
        try:
            render_crop(pdf, pidx, out)
            con.execute(
                "UPDATE facts SET crop=? WHERE fact_id IN (%s)"
                % ",".join("?" * len(by_page[(doc, pidx)])),
                [out] + by_page[(doc, pidx)])
            n += 1  # one render per page, shared by all its facts
        except Exception:
            pass
        if progress and (k % 5 == 0 or k == len(pages) - 1):
            progress("crops", k + 1, total, f"page snapshots {k + 1}/{len(pages)}")
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
