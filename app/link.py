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
# minimum subject similarity for two KNOWN-subject facts to link (mirrored
# by _pair_plan's block merging — keep the two in sync).
SUBJECT_MATCH_MIN = 60


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
    # fuzzy attribute check is the expensive call — skip it when tokens
    # already overlap (verdict-identical: it only gates the no-shared case)
    if not shared and fuzz.token_set_ratio(ta, tb) < 92:
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
    elif fuzz.token_set_ratio(sa, sb) < SUBJECT_MATCH_MIN:
        return False
    if a.fact_type == "numeric" and b.fact_type == "numeric":
        # money-vs-percent explosions end here; convertible units (₹Mn vs ₹Cr)
        # share a dimension and still pass
        if a.unit_norm and b.unit_norm and not same_dimension(a.unit_norm, b.unit_norm):
            return False
    return True


def _pair_plan(facts: list[Fact]) -> tuple[int, object]:
    """Blocking with EXACTLY the recall of the full O(F²) scan.

    same_topic rejects known-subject pairs scoring < SUBJECT_MATCH_MIN, so
    subjects are union-merged at that threshold first and only intra-block
    pairs are emitted. Subjectless/fallback facts (pool) match across
    subjects, so each is paired with every non-pool fact plus pool mates.
    Returns (total_pairs, lazy_iterator).
    """
    n = len(facts)
    pool: list[int] = []
    blocks: dict[str, list[int]] = {}
    for idx, f in enumerate(facts):
        s = (f.subject or "").lower().strip()
        if not s or s in FALLBACK_SUBJECTS:
            pool.append(idx)
        else:
            blocks.setdefault(s, []).append(idx)
    subjects = list(blocks)
    parent = {s: s for s in subjects}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for x in range(len(subjects)):
        for y in range(x + 1, len(subjects)):
            if fuzz.token_set_ratio(subjects[x], subjects[y]) >= SUBJECT_MATCH_MIN:
                rx, ry = find(subjects[x]), find(subjects[y])
                if rx != ry:
                    parent[ry] = rx
    merged: dict[str, list[int]] = {}
    for s in subjects:
        merged.setdefault(find(s), []).extend(blocks[s])
    poolset = set(pool)
    nonpool = [i for i in range(n) if i not in poolset]
    total = (sum(len(v) * (len(v) - 1) // 2 for v in merged.values())
             + len(pool) * len(nonpool) + len(pool) * (len(pool) - 1) // 2) or 1

    def gen():
        for idxs in merged.values():
            for a in range(len(idxs)):
                for b in range(a + 1, len(idxs)):
                    x, y = idxs[a], idxs[b]
                    yield (x, y) if x < y else (y, x)
        for p in pool:
            for q in nonpool:
                yield (p, q) if p < q else (q, p)
        for a in range(len(pool)):
            for b in range(a + 1, len(pool)):
                yield pool[a], pool[b]

    return total, gen()


def _cross_dimension(a: Fact, b: Fact) -> bool:
    """True when both units map to known but different comparability families
    (money vs percent vs days...). Such pairs can never be directly related."""
    from .normalize import UNIT_DIM
    da, db = UNIT_DIM.get(a.unit_norm or ""), UNIT_DIM.get(b.unit_norm or "")
    return bool(da and db and da != db)


def heuristic_link(a: Fact, b: Fact, tol: float) -> dict:
    if a.fact_type == "numeric" and b.fact_type == "numeric" and _cross_dimension(a, b):
        return {}  # veto: incomparable dimensions, no relation at all
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
        return {}  # unverifiable numbers must not "corroborate" — skip
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


VALID_RELATIONS = ("corroborates", "contradicts", "reconciled", "superseded-by")


class LinkBudget:
    """Per-run cap on LLM-as-judge calls.

    Created fresh by link_all on every run. (The old per-process counter
    silently starved every job after the first 80 calls server-wide.)
    """

    def __init__(self, limit: int = 0):
        try:
            from .config import settings as _s
            default = int(getattr(_s, "bedrock_max_link_calls", 80))
        except Exception:
            default = 80
        self.remaining = int(limit) if limit else default

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _candidate_score(a: Fact, b: Fact, tol: float) -> float:
    """Cheap rank for ambiguous pairs so the judge budget is spent on the
    most promising candidates first (shared topic + same period/scope/units)."""
    ta = set(_topic_anchor(a.attribute).split())
    tb = set(_topic_anchor(b.attribute).split())
    score = float(len(ta & tb))
    if a.period and a.period == b.period:
        score += 2.0
    if a.scope and a.scope == b.scope:
        score += 1.0
    if a.unit_norm and a.unit_norm == b.unit_norm:
        score += 1.0
    if (a.fact_type == b.fact_type == "numeric"
            and a.value_norm is not None and b.value_norm is not None):
        denom = max(abs(a.value_norm), abs(b.value_norm), 1e-9)
        if abs(a.value_norm - b.value_norm) / denom <= tol:
            score += 1.5
    return score


def judge_pair(a: Fact, b: Fact, budget: "LinkBudget | None" = None) -> dict:
    """LLM verdict for one ambiguous pair. Pure (no DB); safe to parallelize."""
    if (a.fact_type == "numeric" and b.fact_type == "numeric"
            and _cross_dimension(a, b)):
        return {}  # veto stands even for the LLM judge
    if not llm_available():
        return {}
    if budget is None:
        budget = LinkBudget()
    if not budget.take():
        return {}
    try:
        return llm_link(_slim(a), _slim(b)) or {}
    except Exception:
        return {}


def _clean_axis(v):
    """Coerce the judge's axis to schema. Judges occasionally emit the STRING
    "null"/"none" or an off-schema label — axis is ancillary, so coerce to
    None instead of letting one sloppy token crash the whole link pass."""
    if v is None:
        return None
    s = str(v).strip().lower()
    return s if s in ("time", "scope", "units", "vintage") else None


def _finalize(rel: dict, a: Fact, b: Fact, tol: float,
              disclosures: dict) -> Relation | None:
    """Turn a heuristic/LLM verdict dict into a Relation (or None).

    Applies the vintage-supersession override and the numeric re-check.
    Pure; shared by link_pair and the two-pass link_all.
    """
    if not rel or rel.get("relation") not in VALID_RELATIONS:
        return None  # ambiguous and no verdict — skip, don't fabricate
    rel = dict(rel)
    rel["axis"] = _clean_axis(rel.get("axis"))
    # supersession: same topic, different KNOWN disclosure vintages, conflicting
    # values — the newer disclosure wins (restatement or update), it is not a
    # live conflict. Unknown vintage (0) never supersedes: without evidence of
    # a newer disclosure, same-period conflicts stay contradictions.
    # Different periods with different values stay reconciled (genuine change over time).
    ra, rb = disclosures.get(a.evidence.doc, 0), disclosures.get(b.evidence.doc, 0)
    if ra and rb and ra != rb and rel.get("relation") == "contradicts" and a.period == b.period:
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
        return Relation(relation="superseded-by", axis=rel.get("axis") or "vintage",
                        fact_ids=[a.fact_id, b.fact_id],
                        explanation=rel.get("explanation", ""), confidence=rel.get("confidence", 0.7),
                        verified=rel.get("verified", False))
    return Relation(relation=rel.get("relation", "corroborates"),
                    axis=rel.get("axis"),
                    fact_ids=[a.fact_id, b.fact_id],
                    explanation=rel.get("explanation", ""),
                    confidence=float(rel.get("confidence", 0.5) or 0.5),
                    verified=bool(rel.get("verified", False)))


def link_pair(a: Fact, b: Fact, tol: float, disclosures: dict,
              budget: "LinkBudget | None" = None) -> Relation | None:
    """disclosures: doc -> vintage rank (higher = newer)."""
    rel = heuristic_link(a, b, tol) or {}
    if rel.get("relation") not in VALID_RELATIONS:
        # heuristic ambiguous → LLM judge (budget-capped), never fabricate
        rel = judge_pair(a, b, budget)
    return _finalize(rel, a, b, tol, disclosures)


def _slim(f: Fact) -> dict:
    return {"subject": f.subject, "attribute": f.attribute,
            "value_raw": f.value_raw, "value_norm": f.value_norm,
            "unit_norm": f.unit_norm, "period": f.period, "scope": f.scope,
            "fact_type": f.fact_type,
            "source": f"{f.evidence.doc} p.{f.evidence.page_label}",
            "quote": f.evidence.quote[:400]}
