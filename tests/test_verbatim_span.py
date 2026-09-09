"""Verbatim-span recovery: LLM quotes with reflowed whitespace or markdown
drift are located in the source chunk (stored span stays verbatim);
true paraphrases are still rejected."""
import pytest

from app.chunk import Chunk
from app.extract import (_clean_llm_value, _llm_item_to_fact,
                         _locate_verbatim)


def _chunk(text):
    return Chunk(doc="d.pdf", page_index=0, page_label="1", text=text,
                 modality_hints=[])


def _item(quote, **kw):
    d = {"subject": "s", "attribute": "a", "value_raw": "ZERO",
         "fact_type": "semantic", "confidence": 0.9, "quote": quote,
         "modality": "text"}
    d.update(kw)
    return d


def test_exact_match_unchanged():
    text = "The course will be awarded ZERO marks."
    assert _locate_verbatim("awarded ZERO marks", text) == "awarded ZERO marks"


def test_whitespace_reflow_recovered_verbatim():
    text = "A particular course\nwill be   awarded ZERO\nincluding labs."
    span = _locate_verbatim("A particular course will be awarded ZERO including labs",
                            text)
    assert span is not None
    assert span in text  # stored span is a verbatim substring
    assert "awarded ZERO" in span


def test_markdown_drift_recovered_verbatim():
    text = "| malpractice | **ZERO** for the course |"
    span = _locate_verbatim("malpractice ZERO for the course", text)
    assert span is not None
    assert span in text
    assert "ZERO" in span


def test_paraphrase_rejected():
    text = "The course will be awarded ZERO marks for talking."
    assert _locate_verbatim("Students get full marks for good behaviour", text) is None
    with pytest.raises(ValueError, match="not grounded"):
        _llm_item_to_fact(_item("Students get full marks for good behaviour"),
                          _chunk(text), "Doc")


def test_empty_quote_rejected():
    with pytest.raises(ValueError, match="not grounded"):
        _llm_item_to_fact(_item(""), _chunk("some text"), "Doc")
    with pytest.raises(ValueError, match="not grounded"):
        _llm_item_to_fact(_item("   "), _chunk("some text"), "Doc")


def test_recovered_fact_stores_chunk_span():
    text = "A particular course\nwill be   awarded ZERO\nincluding labs."
    f = _llm_item_to_fact(
        _item("A particular course will be awarded ZERO including labs"),
        _chunk(text), "Doc")
    assert f.evidence.quote in text  # verbatim guarantee holds


def test_cross_chunk_quote_recovered_from_page():
    page = ("Row one says ZERO for talking.\n\n"
            "Row two says a Rs. 3,000 fine for phones.")
    c = Chunk(doc="d.pdf", page_index=0, page_label="1",
              text="Row one says ZERO for talking.", modality_hints=[],
              page_text=page)
    f = _llm_item_to_fact(_item("Row two says a Rs. 3,000 fine for phones."),
                          c, "Doc")
    assert f.evidence.quote in page  # grounded on the same page
    # but a quote from another page is still rejected
    c2 = Chunk(doc="d.pdf", page_index=0, page_label="1",
               text="Row one says ZERO for talking.", modality_hints=[],
               page_text="Row one says ZERO for talking.")
    with pytest.raises(ValueError, match="not grounded"):
        _llm_item_to_fact(_item("Row two says a Rs. 3,000 fine for phones."),
                          c2, "Doc")


def test_value_markdown_stripped():
    assert _clean_llm_value("**ZERO**") == "ZERO"
    assert _clean_llm_value('  "ZERO" ') == "ZERO"
    assert _clean_llm_value("12.7%") == "12.7%"
    assert _clean_llm_value("") == ""
