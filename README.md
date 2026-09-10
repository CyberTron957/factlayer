# Fact Knowledge Layer

LLM-centric system that extracts grounded facts from PDFs, links every fact to
source evidence (quote + page + rendered crop), and classifies cross-document
relationships: **corroborates / contradicts / reconciled-by-context /
superseded-by**. No hard-coded facts, filenames, schemas, or document rules —
the same pipeline runs unmodified on Delhivery filings and on macroeconomy
reports from different publishers.

## Live demo

**http://16.113.49.75:8137/** — the app running on a server, pre-loaded corpus
included. Try it: drag in your own PDFs (progress bar + ETA, safe to refresh
mid-run, cancel keeps partial results), switch **Append ↔ Replace** upload
mode, delete individual documents or clear the corpus, then browse the
★ 4 Cases tab for corroborated / contradiction / reconciled / failure examples
with quote + page + snapshot evidence.

## Setup and Run Instructions

```bash
# 1. Python env (tested 3.12; needs llama-parse, litellm, fastapi, pymupdf, rapidfuzz…)
pip install -r requirements.txt

# 2. Secrets — never committed (.gitignore + pre-commit grep, see scripts/check_no_keys.sh)
export LLAMA_CLOUD_API_KEY="llx-..."      # LlamaParse (primary parser)
export AWS_BEARER_TOKEN_BEDROCK="..."     # Bedrock API key (console: Bedrock → API keys → short-term, 12h TTL)
# optional: BEDROCK_MODEL (default zai.glm-4.7-flash; openai.gpt-5.6-luna = one-line swap once enabled)
#           BEDROCK_REGION (default us-east-1), BEDROCK_MAX_LINK_CALLS (default 80)

# 3. Process PDFs (incremental: unchanged files are skipped by SHA)
python -m scripts.run_corpus            # Delhivery demo corpus (see --help for subsets)
# or: python -c "from app.pipeline import process_files; process_files(['my.pdf'])"

# 4. Serve the UI + API
uvicorn app.main:app --port 8137        # open http://localhost:8137
```

Useful endpoints: `POST /api/upload` (PDFs → returns a job id; processing runs
in background with a progress bar, ETA, and cancel — see `GET /api/jobs`,
`GET /api/jobs/<id>`, `POST /api/jobs/<id>/cancel`; refresh-safe, partial
results are kept on cancel) · `DELETE /api/documents/<name>` (remove one
document + its facts/relations/snapshots) · `DELETE /api/documents` (clear
the whole corpus; both refused with 409 while a job runs) · `GET /api/facts?q=` ·
`GET /api/relations` · `GET /api/timeline` · `GET /api/questions` (open
inbox) · `GET /api/cases` (the four required cases) · `GET /api/export` (CSV).

Tests (no API calls, no PDFs): `python -m pytest tests/ -q` (10 tests: relation
engine, normalizers incl. `(404)`→`-404`, USD-vs-INR dimensions).

## Approach

**Parse (LlamaParse-first).** Default-tier parsing returns per-page markdown with tables
preserved, charts converted to structured series, and page separators that keep
*both* the file index and the printed folio (curated excerpts jump: file page 11
== printed 12). `target_pages` + SHA-keyed cache keep runs cheap and
incremental; PyMuPDF (block-sorted text + `find_tables`) is the keyless fallback.

**Chunk.** Never cross a page boundary (evidence pages stay exact); oversize
tables split row-wise with headers repeated.

**Extract (LLM central, deterministic recall net).** `llm_extract` maps each
chunk to strict-schema JSON `{subject, attribute, value_raw, unit, period,
scope, fact_type, confidence, quote, modality}` — direct HTTPS to the Bedrock
mantle OpenAI-compatible endpoint (`/v1/chat/completions`, no SDK), model from
`BEDROCK_MODEL` env only (GLM 4.7 Flash live; Luna is a one-line swap once AWS
enables the account). Alongside it, a regex/NER pre-pass guarantees baseline
recall with zero LLM cost. Doc entity comes from cover-weighted ORG voting —
no filename/schema knowledge.

**Normalize.** Indian + international numbers, accounting parentheses
(`(404)`→`-404`), unit algebra (`₹8,142 Cr` ≡ `81,415 ₹Mn`; `$…bn` stays USD,
never INR), period canonicalization (`FY24`≡`FY2023-24`, quarters, Fiscal years).

**Link.** Fuzzy blocking proposes candidate pairs (topic anchors minus
period/unit tokens; money-vs-percent can never block together); heuristics
verdict first, `llm_link` judges only ambiguous pairs (budget-capped);
**code re-checks every numeric verdict** (tolerance, dimension
equality) and can overturn the LLM. Fourth relation `superseded-by` separates
"later disclosure wins" (restatements, director active→resigned) from genuine
contradiction.

**Verify.** Quotes must be verbatim substrings of the source chunk; chart-only
facts are capped at medium confidence and queued as provisional; failures land
in the **open-questions inbox**, never silently dropped. Every fact also stores
a rendered page crop as visual evidence.

**Standout bets:** visual evidence crops · temporal versions + "knowledge as
of…" timeline · open-questions inbox as a product surface. AI tools used:
LlamaParse (parse), GLM 4.7 Flash via Bedrock mantle (extract/live-tested link
judge; Luna account-gated — needs AWS Sales enablement, swap is one env var),
coding agent for scaffolding; all prompts are dataset-agnostic (see `app/llm.py`).

## Limitations and Next Steps

- **Case 2 (contradiction) — detector proven, no live specimen**: the Delhivery
  docs genuinely agree (verified by spread audit + macroeconomy run: 0
  contradicts). The detector itself is proven three ways: 12/12 unit tests
  (incl. cross-dimension veto), a live synthetic pair through the full
  `link_pair`→verify path (`contradicts`, verified: True), and precision —
  the nearest real near-misses (cross-period margins) are correctly NOT
  flagged. Feed it disagreeing docs and Case 2 fills itself.
- Cross-publisher macro linking over-links on generic anchors ("GDP" matches
  every metric) — needs IDF-weighted anchors; currently flagged as noisy
  reconciliations, never silently trusted.
- Deterministic period attribution uses nearest-token heuristics (table column
  headers need table-aware resolution — currently flagged `ambiguous-period`).
- Chart legend→segment mapping is positional; ambiguous cases are flagged, not
  guessed. No vision fallback yet (LlamaParse chart parsing sufficed in tests).
- Relations are pairwise; no multi-hop chains. Q&A box and CSV export beyond
  `/api/export` are P1. Full 100-page runs await LlamaParse credit budget
  (subsets used throughout: 27 + 5 + 4 and 4 + 5 + 5 pages).
- Next: Luna swap (one env var, needs AWS Sales enablement) → full-corpus runs
  → calibrated confidence → bridge arithmetic for reconciliations.

## Additional Notes

- Credit discipline: ~50 LlamaParse pages spent total; every parse cached, every
  rebuild free. `sample_output/` lets anyone evaluate without keys.
- `data/` (sqlite, crops, cache) and `.env` are gitignored and rebuilt locally.
- Check `git log` — history is the build diary: scaffold → parse test →
  pipeline → fixes → API/UI → tests → generalization runs.
