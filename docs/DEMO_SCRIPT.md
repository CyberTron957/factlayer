# Demo video shot list (≤ 3 minutes)

0:00–0:20 — Upload: drag the Q4 FY24 earnings deck into the UI (`POST /api/upload`).
0:20–0:50 — Processing: pages → facts → relations counters; open `sample_output/parse_test_3pages.md`
            to show what the parser saw on the KPI/chart pages.
0:50–1:20 — **Case 1 corroborated**: FY24 EBITDA margin 1.6% in deck + annual report, side-by-side
            evidence crops (`/api/cases` → `corroborated`).
1:20–1:50 — **Case 2 contradiction**: docs agree, so demo the live detector instead —
            run the link_pair snippet on a conflicting EBITDA-margin pair (1.6% vs 2.4%,
            same period/scope) → `contradicts, verified: True` with reasoning; then show
            the empty live slot + nearest near-miss the system correctly refused to flag.
1:50–2:20 — **Case 3 reconciled**: EBITDA ₹127 Cr vs Adjusted EBITDA ₹76 Cr — scope axis explained.
2:20–2:45 — **Case 4 failure**: open-questions inbox — chart-provisional fact + handling rule.
2:45–3:00 — Timeline tab ("knowledge as of…") + `GET /api/export` CSV. Done.
