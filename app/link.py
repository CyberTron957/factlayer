"""Linking: cheap fuzzy blocking proposes candidate pairs; LLM classifies;
code re-checks numeric verdicts. Deterministic heuristic covers no-LLM runs.

Extra relation 'superseded-by': same subject+attribute, different disclosure
vintage (period or doc date), newer replaces older — distinct from contradiction.
"""
import re

from rapidfuzz import fuzz

from .config import settings
from .llm import available as llm_available, llm_link
from .models import Fact, Relation
from .normalize import same_dimension
from .verify import _diff_axis, _scope_key, recheck_relation

PERIOD_UNIT_TOKENS = re.compile(
    r"\b(fy\d{0,4}|q[1-4]|yoy|qoq|cr|crs|crore?s?|lakhs?|mn|bn|million|"
    r"billion|rs|inr|usd|per|cent|percent)\b|[₹$%]", re.I)
FALLBACK_SUBJECTS = {"document", "doc", "report", "presentation"}


def _topic_anchor(attr: str) -> str:
    """Attribute stripped of period/unit/generic tokens — the linkable topic."""
    s = PERIOD_UNIT_TOKENS.sub(" ", attr.lower())
    toks = [t for t in re.findall(r"[a-z]{3,}", s)
            if t not in {"the", "and", "for", "from", "with", "was", "are",
                         "that", "this", "these", "those", "which", "such",
                         "than", "then", "also", "into", "over", "about"}]
    return " ".join(toks)


def block_key(f: Fact) -> str:
    return f.subject.lower().strip()


def same_topic(a: Fact, b: Fact) -> bool:
    if a.evidence.doc == b.evidence.doc:
        return False  # cross-document relations only
    if not a.attribute or not b.attribute:
        return False
    ta, tb = _topic_anchor(a.attribute), _topic_anchor(b.attribute)
    if min(len(ta), len(tb)) < 4:
        return False  # no substantive topic (FY-salad, bare units)
    toks_a, toks_b = set(ta.split()), set(tb.split())
    shared = toks_a & toks_b
    attr_score = fuzz.token_set_ratio(ta, tb)
    if not shared and attr_score < 92:
        return False
    sa, sb = a.subject.lower().strip(), b.subject.lower().strip()
    unknown = (sa in FALLBACK_SUBJECTS or sb in FALLBACK_SUBJECTS or not sa or not sb)
    if unknown and a.fact_type == "semantic" and b.fact_type == "semantic":
        # bare claims without subjects: demand strong topic overlap
        if len(shared) < 2 and set(ta.split()) != set(tb.split()):
            return False
    elif unknown:
        if sa in FALLBACK_SUBJECTS and sb in FALLBACK_SUBJECTS:
            if not shared:
                return False  # unknown subjects: topic tokens must overlap
        else:
            # one side subjectless: demand identical topic anchors (strict)
            if set(ta.split()) != set(tb.split()) or not ta:
                return False
    elif fuzz.token_set_ratio(sa, sb) < 60:
        return False
    if a.fact_type == "numeric" and b.fact_type == "numeric":
        # money-vs-percent explosions end here; convertible units (₹Mn vs ₹Cr)
        # share a dimension and still pass
        if a.unit_norm and b.unit_norm and not same_dimension(a.unit_norm, b.unit_norm):
            return False
    return True


def heuristic_link(a: Fact, b: Fact, tol: float) -> dict:
    if (a.fact_type == "numeric") != (b.fact_type == "numeric"):
        # a number and a prose claim corroborate only if the prose cites the number
        num, prose = (a, b) if a.fact_type == "numeric" else (b, a)
        digits = re.sub(r"\D", "", num.value_raw)
        if digits and digits in re.sub(r"\D", "", prose.value_raw + prose.evidence.quote):
            return {"relation": "corroborates", "axis": None,
                    "explanation": (f"Prose claim cites the figure: '{prose.attribute}' "
                                    f"mentions {num.value_raw} ({_ctx(num)})."),
                    "confidence": 0.6}
        return {}  # topic overlap alone is not corroboration — skip
    if a.fact_type != "numeric" or b.fact_type != "numeric":
        return {"relation": "corroborates", "axis": None,
                "explanation": f"Both state '{a.attribute}' similarly ('{a.value_raw}' vs '{b.value_raw}').",
                "confidence": 0.5}
    if a.value_norm is None or b.value_norm is None:
        return {"relation": "corroborates", "axis": None,
                "explanation": "Values not machine-comparable; grouped by topic only.",
                "confidence": 0.35}
    denom = max(abs(a.value_norm), abs(b.value_norm), 1e-9)
    close = abs(a.value_norm - b.value_norm) / denom <= tol
    if a.period != b.period or _scope_key(a) != _scope_key(b) or a.unit_norm != b.unit_norm:
        axis = _diff_axis(a, b)
        if close:
            return {"relation": "corroborates", "axis": axis,
                    "explanation": _explain(a, b, "Same figure expressed with different context: "),
                    "confidence": 0.75}
        return {"relation": "reconciled", "axis": axis,
                "explanation": _explain(a, b, "Figures differ but so does context: "),
                "confidence": 0.7}
    if close:
        return {"relation": "corroborates", "axis": None,
                "explanation": _explain(a, b, "Same period, scope and value: "), "confidence": 0.85}
    # same context but different values: only call it a contradiction when the
    # topic anchors are identical; different phrasing needs the LLM to confirm
    # (else revenue-growth vs EBITDA-margin false-positives). Return None = skip.
    if set(_topic_anchor(a.attribute).split()) == set(_topic_anchor(b.attribute).split()):
        return {"relation": "contradicts", "axis": None,
                "explanation": _explain(a, b, "Same period and scope but different values: "),
                "confidence": 0.7}
    return {}


def _explain(a: Fact, b: Fact, prefix: str) -> str:
    bits = [f"'{a.attribute}' is {a.value_raw} ({_ctx(a)}) vs {b.value_raw} ({_ctx(b)})"]
    return prefix + "; ".join(bits)


def _ctx(f: Fact) -> str:
    parts = [p for p in [f.period, f.scope, f.unit_norm] if p]
    return (", ".join(parts) or "no context") + f" [{f.evidence.doc} p.{f.evidence.page_label}]"


_LINK_CALLS = 0  # per-process LLM-link budget (see settings)


def _link_budget_ok() -> bool:
    global _LINK_CALLS
    try:
        from .config import settings as _s
        limit = int(getattr(_s, "bedrock_max_link_calls", 80))
    except Exception:
        limit = 80
    if _LINK_CALLS >= limit:
        return False
    _LINK_CALLS += 1
    return True


def link_pair(a: Fact, b: Fact, tol: float, disclosures: dict) -> Relation | None:
    """disclosures: doc -> vintage rank (higher = newer)."""
    rel = heuristic_link(a, b, tol) or {}
    if rel.get("relation") not in (
            "corroborates", "contradicts", "reconciled", "superseded-by"):
        # heuristic ambiguous → LLM judge (budget-capped), never fabricate
        rel = {}
        if llm_available() and _link_budget_ok():
            try:
                rel = llm_link(_slim(a), _slim(b)) or {}
            except Exception:
                rel = {}
    if not rel or rel.get("relation") not in (
            "corroborates", "contradicts", "reconciled", "superseded-by"):
        return None  # ambiguous and LLM has no verdict — skip, don't fabricate
    # supersession: same topic, different disclosure vintages, conflicting values —
    # the newer disclosure wins (restatement or update), it is not a live conflict.
    # Different periods with different values stay reconciled (genuine change over time).
    ra, rb = disclosures.get(a.evidence.doc, 0), disclosures.get(b.evidence.doc, 0)
    if ra != rb and rel.get("relation") == "contradicts" and a.period == b.period:
        older, newer = (a, b) if ra < rb else (b, a)
        rel = {"relation": "superseded-by", "axis": "vintage",
               "explanation": (f"{older.value_raw} ({_ctx(older)}) was superseded by "
                               f"{newer.value_raw} ({_ctx(newer)}): later disclosure wins, not a conflict."),
               "confidence": 0.75}
        return Relation(relation="superseded-by", axis="vintage",
                        fact_ids=[older.fact_id, newer.fact_id],
                        explanation=rel["explanation"], confidence=0.75, verified=True)
    rel = recheck_relation(rel, a, b, tol)
    if rel.get("relation") == "superseded-by":
        return Relation(relation="superseded-by", axis=rel.get("axis", "vintage"),
                        fact_ids=[a.fact_id, b.fact_id],
                        explanation=rel.get("explanation", ""), confidence=rel.get("confidence", 0.7),
                        verified=rel.get("verified", False))
    return Relation(relation=rel.get("relation", "corroborates"),
                    axis=rel.get("axis"),
                    fact_ids=[a.fact_id, b.fact_id],
                    explanation=rel.get("explanation", ""),
                    confidence=float(rel.get("confidence", 0.5) or 0.5),
                    verified=bool(rel.get("verified", False)))


def _slim(f: Fact) -> dict:
    return {"subject": f.subject, "attribute": f.attribute,
            "value_raw": f.value_raw, "value_norm": f.value_norm,
            "unit_norm": f.unit_norm, "period": f.period, "scope": f.scope,
            "fact_type": f.fact_type,
            "source": f"{f.evidence.doc} p.{f.evidence.page_label}",
            "quote": f.evidence.quote[:400]}
