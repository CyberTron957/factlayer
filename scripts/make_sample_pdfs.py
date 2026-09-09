"""Generate tiny synthetic PDFs with KNOWN ground truth for end-to-end checks.

Designed relation outcomes (Acme Corp, identical attribute phrasing so the
heuristic linker fires deterministically):
  batch 1: acme-earnings-q4.pdf      (vintage 0)
  batch 2: acme-annual-fy25.pdf      (vintage 0 -> conflicts are contradictions)
  batch 3: acme-prospectus-2022.pdf + acme-restated-2023.pdf (one batch ->
           vintages 0,1 -> same-period conflict is a supersession)

Expected: corroborates (revenue 450 vs 452 Cr FY2025), contradicts (net
profit 52 vs 38 Cr FY2025), reconciled (revenue FY2024 vs FY2025, axis time),
superseded-by (share capital FY2022 100 vs 120 Cr), failure
(canteen sentence -> ambiguous-period / verifier-rejection).

Usage: python scripts/make_sample_pdfs.py  (writes sample_pdfs/*.pdf)
"""
import os

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "sample_pdfs")
STYLES = getSampleStyleSheet()
BODY = STYLES["BodyText"]
H1 = STYLES["Heading1"]


def _table(rows):
    t = Table(rows, colWidths=[9 * cm, 6 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16202e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
    ]))
    return t


def _write(name, title, paras, table=None):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    doc = SimpleDocTemplate(path, pagesize=A4)
    story = [Paragraph(title, H1), Spacer(1, 12)]
    for p in paras:
        story += [Paragraph(p, BODY), Spacer(1, 8)]
    if table:
        story += [Spacer(1, 8), _table(table)]
    doc.build(story)
    print("wrote", path)


def main():
    _write("acme-earnings-q4.pdf", "Acme Corp — Q4 FY2025 earnings update", [
        "Acme Corp announced its fourth quarter results for FY2025.",
        "Total revenue was Rs. 450 Cr in FY2025.",
        "Net profit was Rs. 52 Cr in FY2025.",
        "Employee headcount was 1,200 in FY2025.",
        "The staff canteen serves 500 meals daily.",
    ], table=[["Metric", "FY2025"],
              ["Total revenue", "Rs. 450 Cr"],
              ["Net profit", "Rs. 52 Cr"]])

    _write("acme-annual-fy25.pdf", "Acme Corp — Annual Report FY2025", [
        "Acme Corp annual report for the financial year FY2025.",
        "Total revenue was Rs. 452 Cr in FY2025.",
        "Net profit was Rs. 38 Cr in FY2025.",
        "Employee headcount was 1,205 in FY2025.",
        "Total revenue was Rs. 380 Cr in FY2024.",
    ], table=[["Metric", "FY2025"],
              ["Total revenue", "Rs. 452 Cr"],
              ["Net profit", "Rs. 38 Cr"]])

    _write("acme-prospectus-2022.pdf", "Acme Corp — Prospectus 2022", [
        "Acme Corp draft prospectus filed in 2022.",
        "Share capital was Rs. 100 Cr in FY2022.",
    ])

    _write("acme-restated-2023.pdf", "Acme Corp — Restated accounts 2023", [
        "Acme Corp restated prior-year accounts in 2023.",
        "Share capital was Rs. 120 Cr in FY2022.",
    ])


if __name__ == "__main__":
    main()
