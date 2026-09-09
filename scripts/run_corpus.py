"""Demo corpus runner. Usage:
python -m scripts.run_corpus [--macro] [--db data] [--full]
Default (frugal): Delhivery subsets used in development.
--full parses entire PDFs (costs LlamaParse credits per page).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings

DELHIVERY = [
    ("starter-datasets/delhivery/03-delhivery-q4-fy24-earnings-presentation.pdf", {}, 0),
    ("starter-datasets/delhivery/02-delhivery-annual-report-fy24-excerpt.pdf",
     {"target_pages": "1,3,5,23,84"}, 10),
    ("starter-datasets/delhivery/01-delhivery-prospectus-2022-excerpt.pdf",
     {"target_pages": "37,55,57,86"}, -10),
]
MACRO = [
    ("starter-datasets/india-macroeconomy/01-india-economic-survey-2024-25-excerpt.pdf",
     {"target_pages": "21,37,84,76"}, 0),
    ("starter-datasets/india-macroeconomy/02-rbi-annual-report-2024-25-excerpt.pdf",
     {"target_pages": "7,9,10,94,91"}, 5),
    ("starter-datasets/india-macroeconomy/03-imf-india-2025-article-iv-excerpt.pdf",
     {"target_pages": "2,3,8,63,48"}, 10),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--macro", action="store_true")
    ap.add_argument("--db", default="data", help="DATA_DIR (default: data)")
    ap.add_argument("--full", action="store_true",
                    help="parse whole PDFs (spends credits)")
    args = ap.parse_args()
    settings.data_dir = args.db
    os.environ["DATA_DIR"] = args.db
    from app.pipeline import process_files
    corpus = MACRO if args.macro else DELHIVERY
    for path, kw, vintage in corpus:
        kw = dict(kw)
        if args.full:
            kw.pop("target_pages", None)
        print(">>>", path, kw)
        print(process_files([path], vintage_base=vintage, **kw))


if __name__ == "__main__":
    main()
