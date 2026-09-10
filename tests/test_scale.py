"""Scale guards: blocking recall-parity, per-run link budget, and
parallel-extract determinism. All hermetic (no network): LLM calls are
stubbed, parsing forced to local PyMuPDF."""
import itertools

from app import store as S
from app.config import settings
from app.link import _pair_plan, same_topic
from app.models import Evidence, Fact


def _ev(doc, i=0):
    return Evidence(doc=doc, page_index=i, page_label=str(i + 1),
                    quote="Total revenue was Rs. 450 Cr in FY2025.",
                    modality="text")


def _num(fid, subject, attr, val, doc, period="FY2025"):
    return Fact(fact_id=fid, subject=subject, attribute=attr,
                value_raw=f"Rs. {val} Cr", value_norm=float(val),
                unit_norm="inr_cr", period=period, scope="",
                fact_type="numeric", confidence=0.7, evidence=_ev(doc))


def test_pair_plan_recall_parity():
    """Every pair same_topic could accept must be emitted exactly once."""
    facts = []
    subjects = (["Acme Corp"] * 40 + ["Acme Corporation"] * 30
                + ["Beta Ltd"] * 25 + [""] * 10 + ["document"] * 8)
    for n, subj in enumerate(subjects):
        doc = "a.pdf" if n % 2 == 0 else "b.pdf"
        attr = f"total revenue stream {n % 7}" if n % 3 else f"headcount region {n % 5}"
        facts.append(_num(f"f-{n:04d}", subj, attr, 100 + n, doc))
    # same-doc pairs can never link but must not break the planner
    facts.append(_num("f-x1", "Acme Corp", "total revenue stream 1", 111, "a.pdf"))
    facts.append(_num("f-x2", "Acme Corp", "total revenue stream 1", 112, "a.pdf"))

    total, it = _pair_plan(facts)
    emitted = list(it)
    assert len(emitted) == len(set(emitted))  # no duplicates
    assert all(i < j for i, j in emitted)
    assert total == len(emitted)
    n = len(facts)
    for i, j in itertools.combinations(range(n), 2):
        if same_topic(facts[i], facts[j]):
            assert (i, j) in set(emitted)


def _seed_ambiguous(con, n_pairs=100):
    S.upsert_document(con, "a.pdf", "sha-a", 1, "Acme Corp", 2025)
    S.upsert_document(con, "b.pdf", "sha-b", 1, "Acme Corp", 2025)
    for k in range(n_pairs):
        # same period/scope/units, different values, non-identical anchors:
        # heuristic_link returns {} so the LLM judge decides.
        S.insert_fact(con, _num(f"a-{k:04d}", "Acme Corp",
                                f"total revenue alpha {k}", 100 + k, "a.pdf"))
        S.insert_fact(con, _num(f"b-{k:04d}", "Acme Corp",
                                f"revenue growth alpha {k}", 900 + k, "b.pdf"))


def _relations_key(con):
    rows = con.execute(
        "SELECT relation, fact_a, fact_b FROM relations").fetchall()
    return sorted((r, tuple(sorted((a, b)))) for r, a, b in rows)


def test_link_budget_resets_every_run(monkeypatch, tmp_path):
    """A second link_all on the same DB must judge just like the first
    (regression: the old per-process counter starved every later run)."""
    import app.link as L
    from app.pipeline import link_all

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "bedrock_max_link_calls", 30)
    calls = []

    def fake_link(fa, fb):
        calls.append((fa["attribute"], fb["attribute"]))
        return {"relation": "contradicts", "axis": None,
                "explanation": "stub", "confidence": 0.9}

    monkeypatch.setattr(L, "llm_available", lambda: True)
    monkeypatch.setattr(L, "llm_link", fake_link)

    con = S.connect()
    _seed_ambiguous(con)
    out1 = link_all(con)
    key1, n_calls_1 = _relations_key(con), len(calls)
    assert out1["relations"] == 30 == n_calls_1 == len(key1)

    out2 = link_all(con)
    key2 = _relations_key(con)
    assert out2["relations"] == 30
    assert len(calls) == 2 * n_calls_1  # judge ran again — budget reset
    assert key1 == key2  # identical verdicts, deterministic rank order
    con.close()


def _write_pdf(path, pages):
    import fitz
    d = fitz.open()
    for text in pages:
        p = d.new_page()
        p.insert_text((72, 72), text)
    d.save(path)
    d.close()


def test_parallel_extract_deterministic(monkeypatch, tmp_path):
    """workers=1 vs workers=8 yield identical facts; a cleared-DB rerun is
    served from caches without a single LLM call."""
    from app import pipeline as P

    # NB: no "Rs." abbreviation — its period splits sentences mid-figure.
    profitable = ("Acme Corp announced results. Total revenue was 450 Cr "
                  "in FY2025. Net profit was 52 Cr in FY2025.")
    filler = ("Contents Overview Summary Appendix Glossary Acknowledgements "
              "Preface Foreword Chapters Sections Notes References Index")
    docs = {
        "w1.pdf": [profitable, filler],
        "w2.pdf": [profitable.replace("450", "452").replace("52", "38"), filler],
        "w3.pdf": [filler, profitable],
    }
    orig_parse = P.parse_pdf
    monkeypatch.setattr(P, "parse_pdf",
                        lambda path, target=None: orig_parse(path, target, force="pypdf"))
    import app.extract as E
    llm_calls: list = []
    monkeypatch.setattr(E, "llm_extract",
                        lambda text, doc: llm_calls.append(doc) or [])

    def run_in(d, workers):
        d.mkdir(exist_ok=True)
        monkeypatch.setattr(settings, "data_dir", str(d))
        monkeypatch.setattr(settings, "extract_workers", workers)
        paths = []
        for name, pages in docs.items():
            p = d / name
            if not p.exists():  # keep bytes stable: rewrites change the SHA
                _write_pdf(str(p), pages)
            paths.append(str(p))
        stats = P.process_files(paths)
        con = S.connect()
        rows = con.execute(
            "SELECT fact_id,subject,attribute,value_raw,period,quote FROM facts"
            " ORDER BY fact_id").fetchall()
        con.close()
        return stats, rows

    stats1, rows1 = run_in(tmp_path / "a", 1)
    assert stats1["docs"] == 3 and stats1["facts"] > 0
    assert llm_calls, "signal pages must reach the LLM"
    cache_files = list((tmp_path / "a" / "extract_cache").glob("*.json"))
    assert cache_files, "extract cache should persist chunk results"

    llm_calls.clear()
    stats2, rows2 = run_in(tmp_path / "b", 8)
    assert (stats1, rows1) == (stats2, rows2)  # parallelism is deterministic

    # wipe the DB (caches survive by design) and rerun: zero LLM calls,
    # identical facts — everything came from parse + extract caches.
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "b"))
    S.clear_all()
    llm_calls.clear()
    stats3, rows3 = run_in(tmp_path / "b", 8)
    assert llm_calls == []
    assert (stats1, rows1) == (stats3, rows3)
