"""Frugal LlamaParse smoke test: 3 hardest earnings-deck pages only.

Pages (0-based excerpt indices): 5 = KPI infographic, 11 = EBITDA chart,
14 = Financial Performance dashboard. Saves markdown for inspection;
commits as a no-key-needed sample artifact.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llama_parse import LlamaParse

PDF = "starter-datasets/delhivery/03-delhivery-q4-fy24-earnings-presentation.pdf"
OUT = "sample_output/parse_test_3pages.md"

parser = LlamaParse(
    api_key=os.environ["LLAMA_CLOUD_API_KEY"],
    result_type="markdown",
    target_pages="5,11,14",
    page_separator="\n== PAGE {pageNumber} ==\n",
    verbose=True,
)
docs = parser.load_data(PDF)
print(f"documents returned: {len(docs)}")
for i, d in enumerate(docs):
    print(f"--- doc {i}: {len(d.text)} chars; metadata={d.metadata}")
full = "\n\n".join(d.text for d in docs)
with open(OUT, "w") as f:
    f.write(full)
print(f"saved {len(full)} chars -> {OUT}")
