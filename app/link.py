"""Linking: cheap fuzzy blocking proposes candidate pairs; LLM classifies;
code re-checks numeric verdicts. Deterministic heuristic covers no-LLM runs.

Extra relation 'superseded-by': same subject+attribute, different disclosure
vintage (period or doc date), newer replaces older — distinct from contradiction.
"""
from rapidfuzz import fuzz

from .config import settings
from .llm import available as llm_available, llm_link
from .models import Fact, Relation
from .normalize import same_dimension
from .verify import _diff_axis, _scope_key, recheck_relation


def block_key(f: Fact) -> str:
    return f.subject.lower().strip()


def same_topic(a: Fact, b: Fact) -> bool:
    if a.evidence.doc == b.evidence.doc:
        return False  # cross-document relations only
    if not a.attribute or not b.attribute:
        return False
    attr = fuzz.token_set_ratio(a.attribute.lower(), b.attribute.lower())
    subj = fuzz.token_set_ratio(a.subject.lower(), b.subject.lower())
    if attr < settings.fuzzy_threshold or subj < 60:
        return False
    if a.fact_type == "numeric" and b.fact_type == "numeric":
        if a.unit_norm and b.unit_norm and not same_dimension(a.unit_norm, b.unit_norm):
            # allow: may still reconcile across units — keep as candidate
            pass
    return True


def heuristic_link(a: Fact, b: Fact, tol: float) -> dict:
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
    return {"relation": "contradicts", "axis": None,
            "explanation": _explain(a, b, "Same period and scope but different values: "),
            "confidence": 0.7}


def _explain(a: Fact, b: Fact, prefix: str) -> str:
    bits = [f"'{a.attribute}' is {a.value_raw} ({_ctx(a)}) vs {b.value_raw} ({_ctx(b)})"]
    return prefix + "; ".join(bits)


def _ctx(f: Fact) -> str:
    parts = [p for p in [f.period, f.scope, f.unit_norm] if p]
    return (", ".join(parts) or "no context") + f" [{f.evidence.doc} p.{f.evidence.page_label}]"


def link_pair(a: Fact, b: Fact, tol: float, disclosures: dict) -> Relation | None:
    """disclosures: doc -> vintage rank (higher = newer)."""
    rel: dict = {}
    if llm_available():
        try:
            rel = llm_link(_slim(a), _slim(b)) or {}
        except Exception:
            rel = {}
    if not rel or rel.get("relation") not in (
            "corroborates", "contradicts", "reconciled", "superseded-by"):
        rel = heuristic_link(a, b, tol)
    # supersession: same topic, newer vintage, different value context
    ra, rb = disclosures.get(a.evidence.doc, 0), disclosures.get(b.evidence.doc, 0)
    if ra != rb and rel.get("relation") == "contradicts" and a.period != b.period:
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
