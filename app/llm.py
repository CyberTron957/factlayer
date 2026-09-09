"""LLM layer (central component). All calls go through LiteLLM's
OpenAI-compatible interface; model comes from LITELLM_MODEL env only.

Two fenced calls: EXTRACT (chunk -> facts JSON) and LINK (fact pair ->
relation JSON). Both outputs are Pydantic-validated and code-verified
downstream; the LLM never writes to storage directly.
"""
import json
from typing import Any

from .config import settings


EXTRACT_SYSTEM = """You extract grounded facts from document chunks. Rules:
- Output ONLY a JSON array of fact objects. No prose.
- Every fact MUST include "quote": a verbatim substring of the chunk text supporting it.
- "subject": the entity the fact is about (company, institution, country...). Use the document's main entity when the chunk implies it.
- "attribute": the metric/property in plain words (e.g. "revenue from services", "workforce headcount", "policy repo rate").
- Numeric facts: "value_raw" exactly as written (keep symbols/commas/parentheses), "unit" as written (Cr, %, Mn tons...), "period" as written (FY24, Q4 FY24...), "scope" any qualifier (adjusted, standalone, consolidated...).
- Semantic facts (appointments, vision, status, rankings): fact_type "semantic", value_raw = the claim in ≤20 words, unit "".
- "fact_type" is "numeric" or "semantic". "confidence" 0..1. "modality": text|table|chart|infographic.
- Parenthesized numbers like (404) are accounting negatives — keep them verbatim in value_raw.
- Skip tables of contents, page numbers, headers/footers. Prefer precision over recall.
- Never invent periods, units, or values. If absent, use "".
Schema per fact: {"subject":str,"attribute":str,"value_raw":str,"unit":str,"period":str,"scope":str,"fact_type":str,"confidence":float,"quote":str,"modality":str}"""

LINK_SYSTEM = """You compare two normalized facts about the same topic from different documents. Rules:
- Output ONLY one JSON object. No prose.
- "relation": "corroborates" (same claim, values agree), "contradicts" (same period+scope, values disagree), "reconciled" (values differ BUT periods/scopes/units differ — name the axis), "superseded-by" (older disclosure replaced by a newer one; fact_ids order [older, newer]).
- "axis": one of time|scope|units|vintage|null.
- "explanation": ≤60 words, citing the concrete differing qualifiers (periods, scopes, units). No dataset trivia.
- "confidence" 0..1.
Schema: {"relation":str,"axis":str|null,"explanation":str,"confidence":float}"""


def available() -> bool:
    return bool(settings.litellm_model)


def _chat(system: str, user: str, max_tokens: int = 2000) -> str:
    import litellm
    kwargs: dict[str, Any] = {
        "model": settings.litellm_model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    if settings.openai_api_key:
        kwargs["api_key"] = settings.openai_api_key
    resp = litellm.completion(**kwargs)
    return resp.choices[0].message.content or ""


def _parse_json_array(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[1] if "\n" in text.strip("`") else ""
        if text.endswith("```"):
            text = text[:-3]
    try:
        out = json.loads(text)
        return out if isinstance(out, list) else []
    except Exception:
        # salvage: outermost [...] slice
        try:
            s, e = text.index("["), text.rindex("]")
            out = json.loads(text[s:e + 1])
            return out if isinstance(out, list) else []
        except Exception:
            return []


def _parse_json_obj(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        inner = text.strip("`")
        text = inner.split("\n", 1)[1] if "\n" in inner else inner
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else {}
    except Exception:
        try:
            s, e = text.index("{"), text.rindex("}")
            out = json.loads(text[s:e + 1])
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}


def llm_extract(chunk_text: str, doc_hint: str) -> list[dict]:
    if not available():
        return []
    user = f"Document: {doc_hint}\n\nCHUNK:\n{chunk_text}"
    return _parse_json_array(_chat(EXTRACT_SYSTEM, user))


def llm_link(fact_a: dict, fact_b: dict) -> dict:
    if not available():
        return {}
    user = ("FACT A:\n" + json.dumps(fact_a, ensure_ascii=False)
            + "\n\nFACT B:\n" + json.dumps(fact_b, ensure_ascii=False))
    return _parse_json_obj(_chat(LINK_SYSTEM, user, max_tokens=500))
