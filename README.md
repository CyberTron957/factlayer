# Fact Knowledge Layer

Grounded facts from PDFs — every claim linked to a **verbatim quote + page + screenshot**, with **cross-document relations** (agree / disagree / differ-by-context / superseded). No hard-coded facts, filenames, or schemas: the same pipeline runs unmodified on company filings and macroeconomy reports.

## Try it live (30 seconds)

**http://16.113.49.75:8137/** — pre-loaded, nothing to upload.

1. Open the link. The **★ 4 Cases** tab is already full — that's the whole demo.
2. **Case 1**: RBI says real GDP growth 6.5%, IMF says 6.6% (2025-26) → corroborated, both quotes + page snapshots shown.
3. **Case 2**: services-trade growth 7% vs goods+services 3.8% → contradiction, side by side.
4. **Case 3**: same metric, different years → reconciled with the axis named (`time`).
5. **Case 4**: what the system *couldn't* ground, and what it did instead of guessing.
6. Got your own PDFs? Drop them in the Corpus box — Append adds to the demo, Replace starts fresh. Progress bar + ETA, safe to refresh mid-run.

## What's inside

| Tab | Contents (live corpus) |
|---|---|
| ★ 4 Cases | Best corroboration / contradiction / reconciliation / failure, each with evidence |
| Facts | 4,286 grounded facts, filterable by text, type, quality |
| Relations | 5,129 links (1,595 corroborate · 27 contradict · 3,507 reconcile) |
| Timeline | Facts by period + supersessions (newer disclosure wins) |
| Open questions | 3,160-item inbox: ambiguous periods, chart-only facts, rejected quotes — failures are shown, never hidden |

Pre-loaded corpus: Economic Survey 2024-25 (89 pp) · RBI Annual Report 2024-25 (100 pp) · IMF India Article IV 2025 (95 pp).

## Run it locally

```bash
pip install -r requirements.txt      # Python 3.12

# keys (never committed — gitignored, see scripts/check_no_keys.sh)
export LLAMA_CLOUD_API_KEY="llx-..."        # cloud parser (optional; PyMuPDF fallback built in)
export AWS_BEARER_TOKEN_BEDROCK="..."       # Bedrock API key (Bedrock → API keys, ~12h TTL)
# optional: BEDROCK_MODEL (default zai.glm-4.7-flash), BEDROCK_MAX_LINK_CALLS (default 80)

uvicorn app.main:app --port 8137           # open http://localhost:8137
```

Process PDFs headlessly (incremental — unchanged files skip by SHA):

```bash
python -m scripts.run_corpus --macro --db data_macro --full   # full India-macro run
python -m scripts.run_corpus --help                           # subsets, other corpora
```

Which database the server shows is one env var: `DATA_DIR=data_macro` (default `data`). Wiped the demo by accident? `./scripts/restore_demo_corpus.sh` brings it back from the snapshot.

## How it works

```
PDFs → Parse → Chunk → Extract → Normalize → Link → Verify → UI
```

- **Parse** — LlamaParse first (per-page markdown, tables kept, printed folio preserved), PyMuPDF fallback (`find_tables`, block-sorted text). SHA-cached, so re-runs are free.
- **Chunk** — never crosses a page boundary, so every fact's evidence page is exact. Oversize tables split row-wise with headers repeated.
- **Extract (LLM is central)** — one strict-schema call per chunk (`subject, attribute, value_raw, unit, period, scope, fact_type, confidence, quote, modality`) via direct HTTPS to Bedrock's OpenAI-compatible endpoint (no SDK; model is env-only). A regex/NER pre-pass guarantees baseline recall at zero LLM cost.
- **Normalize** — Indian + international numbers, accounting parentheses (`(404)`→`-404`), unit algebra (`₹8,142 Cr` ≡ `81,415 ₹Mn`; `$…bn` stays USD, never INR), period canonicalization (`FY24`≡`FY2023-24`).
- **Link** — fuzzy blocking proposes pairs (money can never block with percent); heuristics verdict first, the LLM judges only ambiguous pairs (budget-capped, best candidates first). Passes are parallel; reruns are deterministic.
- **Verify** — quotes must be verbatim substrings of the source; **every numeric verdict is code re-checked** (tolerance, dimension equality) and the code can overturn the LLM. Chart-only facts are capped at medium confidence and queued as provisional.
- **Evidence** — every fact stores a rendered page crop; open `GET /api/crop?path=…` to see exactly what the extractor saw.

Standout bets: page snapshots as visual evidence · "knowledge as of…" timeline · the open-questions inbox as a product surface (the system shows its work *and* its doubts).

## API cheat sheet

| Call | What it does |
|---|---|
| `POST /api/upload` | PDFs → background job (progress, ETA, cancel; refresh-safe) |
| `GET /api/jobs`, `GET /api/jobs/<id>`, `POST /api/jobs/<id>/cancel` | Track / stop work |
| `DELETE /api/documents/<name>`, `DELETE /api/documents` | Remove one doc or clear all (409 while a job runs) |
| `GET /api/facts?q=` · `/api/relations` · `/api/timeline` · `/api/questions` · `/api/cases` | Browse everything the UI shows |
| `GET /api/export` | Facts CSV + relations JSON |

## Project map

```
app/            parse.py · chunk.py · extract.py (+regex pre-pass) · llm.py (Bedrock)
                normalize.py (numbers/units/periods) · link.py (block+heuristics+judge)
                verify.py · pipeline.py (orchestration) · jobs.py (background jobs)
                store.py (sqlite) · main.py (API) · config.py (all settings, env-only)
static/         index.html — the whole UI (no build step)
scripts/        run_corpus.py (batch runs) · backfill_value_norm.py · restore_demo_corpus.sh
starter-datasets/ delhivery/ · india-macroeconomy/ (the source PDFs + provenance READMEs)
tests/          40 tests, no API calls, no PDFs — relations, normalizers, jobs,
                verbatim-span grounding, scale/parity (blocking ≡ full scan, reruns identical)
sample_output/  example API payloads for evaluation without keys
docs/           DEMO_SCRIPT.md — the ≤3-minute video shot list
```

Runtime state (`data*/`: sqlite, crops, caches) and `.env` are gitignored and rebuilt locally — `git log` is the build diary.

## Limitations (honest)

- **Recall is deliberately precision-biased**: the extractor skips what it can't quote exactly; the misses land in Open questions rather than as hallucinations.
- **Relations are pairwise** — no multi-hop chains yet.
- **Table-period attribution is heuristic** (nearest tokens); column-header-aware resolution would shrink the 2,468 `ambiguous-period` items.
- **Chart reading is positional** (legend→segment mapping); ambiguous cases are flagged provisional, no vision fallback yet.
- Period/unit edge cases in non-English phrasing will still slip through — the verifier catches most, the inbox catches the rest.
