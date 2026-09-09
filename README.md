# Fact Knowledge Layer

LLM-centric system that extracts grounded facts from PDFs, links every fact to
source evidence (quote + page + rendered crop), and classifies cross-document
relationships: **corroborates / contradicts / reconciled-by-context /
superseded-by**. No hard-coded facts, filenames, schemas, or document rules —
the same pipeline runs unmodified on Delhivery filings and on macroeconomy
reports from different publishers.

## Setup and Run Instructions

```bash
# 1. Python env (tested 3.12; needs llama-parse, litellm, fastapi, pymupdf, rapidfuzz…)
pip install -r requirements.txt

# 2. Secrets — never committed (.gitignore + pre-commit grep, see scripts/check_no_keys.sh)
export LLAMA_CLOUD_API_KEY="llx-..."      # LlamaParse (primary parser)
export LITELLM_MODEL="..."                # OpenAI-compatible model via LiteLLM
# optional: export OPENAI_BASE_URL / OPENAI_API_KEY for a custom gateway

# 3. Process PDFs (incremental: unchanged files are skipped by SHA)
python -m scripts.run_corpus            # Delhivery demo corpus (see --help for subsets)
# or: python -c "from app.pipeline import process_files; process_files(['my.pdf'])"

# 4. Serve the UI + API
uvicorn app.main:app --port 8137        # open http://localhost:8137
```

Useful endpoints: `POST /api/upload` (PDFs) · `GET /api/facts?q=` ·
`GET /api/relations` · `GET /api/timeline` · `GET /api/questions` (open
inbox) · `GET /api/cases` (the four required cases) · `GET /api/export` (CSV).

Tests (no API calls, no PDFs): `python -m pytest tests/ -q` (10 tests: relation
engine, normalizers incl. `(404)`→`-404`, USD-vs-INR dimensions).

## Video Demo

`docs/DEMO_SCRIPT.md` is the ≤3-minute shot list (upload → processing → the four
cases on screen). Record with any screen recorder against a local run; the
`sample_output/` JSON files are the exact API payloads shown.

## Approach

**Parse (LlamaParse-first).** Agentic tier returns per-page markdown with tables
preserved, charts converted to structured series, and page separators that keep
*both* the file index and the printed folio (curated excerpts jump: file page 11
== printed 12). `target_pages` + SHA-keyed cache keep runs cheap and
incremental; PyMuPDF (block-sorted text + `find_tables`) is the keyless fallback.

**Chunk.** Never cross a page boundary (evidence pages stay exact); oversize
tables split row-wise with headers repeated.

**Extract (LLM central, deterministic recall net).** `llm_extract` maps each
chunk to strict-schema JSON `{subject, attribute, value_raw, unit, period,
scope, fact_type, confidence, quote, modality}`. Alongside it, a regex/NER
pre-pass guarantees baseline recall with zero LLM cost. Doc entity comes from
cover-weighted ORG voting — no filename/schema knowledge.

**Normalize.** Indian + international numbers, accounting parentheses
(`(404)`→`-404`), unit algebra (`₹8,142 Cr` ≡ `81,415 ₹Mn`; `$…bn` stays USD,
never INR), period canonicalization (`FY24`≡`FY2023-24`, quarters, Fiscal years).

**Link.** Fuzzy blocking proposes candidate pairs (topic anchors minus
period/unit tokens; money-vs-percent can never block together); `llm_link`
classifies; **code re-checks every numeric verdict** (tolerance, dimension
equality) and can overturn the LLM. Fourth relation `superseded-by` separates
"later disclosure wins" (restatements, director active→resigned) from genuine
contradiction.

**Verify.** Quotes must be verbatim substrings of the source chunk; chart-only
facts are capped at medium confidence and queued as provisional; failures land
in the **open-questions inbox**, never silently dropped. Every fact also stores
a rendered page crop as visual evidence.

**Standout bets:** visual evidence crops · temporal versions + "knowledge as
of…" timeline · open-questions inbox as a product surface. AI tools used:
LlamaParse (parse), a LiteLLM-served LLM (extract/link — model TBD), coding
agent for scaffolding; all prompts are dataset-agnostic (see `app/llm.py`).

## Limitations and Next Steps

- **Contradiction on live data is verdict-ready but untriggered**: the engine
  (tests: 10/10) emits `contradicts`, but the current deterministic attributes
  are too noisy to surface a true same-context conflict — needs LLM attributes
  or full-document parses. Same for cross-publisher macro links (MB, RBI/IMF
  phrasing variance). This is the first thing `LITELLM_MODEL` unlocks.
- Deterministic period attribution uses nearest-token heuristics (table column
  headers need table-aware resolution — currently flagged `ambiguous-period`).
- Chart legend→segment mapping is positional; ambiguous cases are flagged, not
  guessed. No vision fallback yet (LlamaParse chart parsing sufficed in tests).
- Relations are pairwise; no multi-hop chains. Q&A box and CSV export beyond
  `/api/export` are P1. Full 100-page runs await the model + credit budget
  (subsets used throughout: 27 + 5 + 4 and 4 + 5 + 5 pages).
- Next: plug in model → full-corpus runs → calibrated confidence → bridge
  arithmetic for reconciliations (show the `127 − x = 76` math).

## Additional Notes

- Credit discipline: ~50 LlamaParse pages spent total; every parse cached, every
  rebuild free. `sample_output/` lets anyone evaluate without keys.
- `data/` (sqlite, crops, cache) and `.env` are gitignored and rebuilt locally.
- Check `git log` — history is the build diary: scaffold → parse test →
  pipeline → fixes → API/UI → tests → generalization runs.
