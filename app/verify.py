"""Verifier: every stored fact must be grounded; every numeric relation
verdict must survive a code re-check. Failures go to the open-questions
inbox instead of being silently dropped or stored."""
from .chunk import Chunk
from .models import Fact
from .normalize import same_dimension


def verify_fact(f: Fact, chunk_by_page: dict) -> tuple[bool, str]:
    """Check quote grounding against the source chunk text."""
    key = (f.evidence.page_index)
    chunk_text = chunk_by_page.get(key, "")
    if not f.evidence.quote or f.evidence.quote not in chunk_text:
        return False, "quote not found verbatim in source page"
    if f.fact_type == "numeric" and f.value_norm is None and not f.flags:
        return False, "numeric fact without parseable value"
    return True, "ok"


def recheck_relation(rel: dict, a: Fact, b: Fact, tol: float) -> dict:
    """Code re-check of an LLM (or heuristic) verdict over normalized numbers."""
    out = dict(rel)
    if a.fact_type != "numeric" or b.fact_type != "numeric":
        out["verified"] = True  # semantic verdicts stand on LLM + similarity
        return out
    if a.value_norm is None or b.value_norm is None:
        out["verified"] = False
        out.setdefault("explanation", "")
        out["explanation"] += " [unverified: unparseable value]"
        return out
    if not same_dimension(a.unit_norm, b.unit_norm):
        # different dimensions can only reconcile, never corroborate/contradict
        if out.get("relation") in ("corroborates", "contradicts"):
            out["relation"] = "reconciled"
            out["axis"] = "units"
            out["explanation"] = (out.get("explanation", "")
                + f" [corrected: {a.unit_norm} vs {b.unit_norm} are different dimensions]")
        out["verified"] = True
        return out
    denom = max(abs(a.value_norm), abs(b.value_norm), 1e-9)
    close = abs(a.value_norm - b.value_norm) / denom <= tol
    same_ctx = (a.period == b.period and a.period != "") and _scope_key(a) == _scope_key(b)
    verdict = out.get("relation")
    if close and verdict == "contradicts" and same_ctx:
        pass  # genuinely contradictory numbers stand
    if close and verdict == "contradicts" and not same_ctx:
        out["relation"] = "reconciled"
        out["axis"] = out.get("axis") or _diff_axis(a, b)
        out["verified"] = True
        return out
    if not close and verdict == "corroborates" and same_ctx:
        out["relation"] = "contradicts"
        out["explanation"] = (out.get("explanation", "")
            + f" [corrected: {a.value_raw} vs {b.value_raw} differ beyond tolerance]")
        out["verified"] = True
        return out
    out["verified"] = True
    return out


def _scope_key(f: Fact) -> str:
    return f.scope.lower().strip()


def _diff_axis(a: Fact, b: Fact) -> str:
    if a.period != b.period:
        return "time"
    if _scope_key(a) != _scope_key(b):
        return "scope"
    if a.unit_norm != b.unit_norm:
        return "units"
    return "vintage"
