"""Scale bench: 100 synthetic pages (4 docs mirroring the Acme corpus) through
the FULL pipeline with real Bedrock calls. Parsing forced to local PyMuPDF
(parse quality is orthogonal to this bench; swap force to auto for LlamaParse).

Asserts the 4 showcase verdicts survive at scale + run-to-run identical
verdicts (end-to-end link-budget-reset proof). Prints per-stage timings.

Usage: python scripts/bench_scale.py  (needs AWS_BEARER_TOKEN_BEDROCK)
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reportlab.lib.pagesizes import A4
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
from reportlab.lib.styles import getSampleStyleSheet

from app.config import settings

BODY = getSampleStyleSheet()["BodyText"]
H1 = getSampleStyleSheet()["Heading1"]

FILLER = ("Contents Overview Summary Appendix Glossary Acknowledgements "
          "Preface Foreword Chapters Sections Notes References Index")


def _pages(sentences):
    story = []
    for s in sentences:
        story += [Paragraph(s, BODY), Spacer(1, 12), PageBreak()]
    return story[:-1]


def _write(path, title, sentences):
    doc = SimpleDocTemplate(path, pagesize=A4)
    doc.build([Paragraph(title, H1), Spacer(1, 12)] + _pages(sentences))
    print(f"wrote {path} ({len(sentences)} pages)")


def build(root):
    a, b, c, d = (os.path.join(root, n) for n in
                  ("bench-q4.pdf", "bench-annual.pdf",
                   "bench-prospectus.pdf", "bench-restated.pdf"))
    a_s = ["Acme Corp announced fourth quarter results for FY2025.",
           "Total revenue was 450 Cr in FY2025.",
           "Net profit was 52 Cr in FY2025.",
           "Employee headcount was 1200 in FY2025.",
           "The staff canteen serves 500 meals daily."]
    b_s = ["Acme Corp annual report for the financial year FY2025.",
           "Total revenue was 452 Cr in FY2025.",
           "Net profit was 38 Cr in FY2025.",
           "Employee headcount was 1205 in FY2025.",
           "Total revenue was 380 Cr in FY2024."]
    c_s = ["Acme Corp draft prospectus filed in 2022.",
           "Share capital was 100 Cr in FY2022."]
    d_s = ["Acme Corp restated prior-year accounts in 2023.",
           "Share capital was 120 Cr in FY2022."]
    for i in range(36):
        a_s.append(f"Consolidated iron ore shipments were {500 + i} tons in FY2025.")
        b_s.append(f"Annual employee training days were {1200 + i} days in FY2025.")
        if i % 4 == 0:
            a_s.append(FILLER)
            b_s.append(FILLER)
    for i in range(8):
        c_s.append(" Risk factors general market conditions disclosure statement.")
        d_s.append(" Restatement notes general market conditions disclosure statement.")
    _write(a, "Acme Corp — Q4 FY2025 bench update", a_s)
    _write(b, "Acme Corp — Annual Report FY2025 bench", b_s)
    _write(c, "Acme Corp — Prospectus 2022 bench", c_s)
    _write(d, "Acme Corp — Restated accounts 2023 bench", d_s)
    return [a, b, c, d]


def main():
    from app.llm import _chat
    try:
        _chat("Reply with one word.", "say ok", max_tokens=5)
    except Exception as e:
        print(f"LLM key dead ({type(e).__name__}: {e}) — aborting bench.")
        sys.exit(2)

    tmp = tempfile.mkdtemp(prefix="bench_scale_")
    settings.data_dir = os.path.join(tmp, "data")
    paths = build(tmp)

    from app import pipeline as P
    orig_parse = P.parse_pdf
    P.parse_pdf = lambda path, target=None: orig_parse(path, target, force="pypdf")
    from app import store as S

    def timed_run(tag):
        marks = {}
        def prog(stage, done, total, detail=""):
            marks[stage] = time.time()
        t0 = time.time()
        stats = P.process_files(paths, progress=prog)
        dt = time.time() - t0
        con = S.connect()
        rels = sorted((r[0], r[1] or "") for r in
                      con.execute("SELECT relation, axis FROM relations"))
        kinds = sorted(r[0] for r in con.execute("SELECT DISTINCT kind FROM open_questions"))
        con.close()
        print(f"[{tag}] {dt:.0f}s stats={stats}")
        print(f"[{tag}] relations={rels}")
        print(f"[{tag}] question-kinds={kinds}")
        return stats, rels, kinds

    r1 = timed_run("run1")
    r2 = timed_run("run2-skipped")  # unchanged SHAs: everything skipped
    assert r2[0]["docs"] == 0, "unchanged docs must skip"
    S.clear_all()  # wipe DB, caches survive
    r3 = timed_run("run3-from-cache")
    assert r1[1] == r3[1] and r1[2] == r3[2], "verdicts must match post-wipe rerun"

    kinds = {r for r, _ in r1[1]}
    assert "corroborates" in kinds, "revenue corroboration lost at scale"
    assert "contradicts" in kinds, "profit contradiction lost at scale"
    assert "reconciled" in kinds, "FY24/FY25 reconciliation lost at scale"
    assert "superseded-by" in kinds, "share-capital supersession lost at scale"
    assert "ambiguous-period" in r1[2], "canteen failure case lost at scale"
    print("BENCH OK — all showcase verdicts survive at 100 pages, runs deterministic")


if __name__ == "__main__":
    main()
