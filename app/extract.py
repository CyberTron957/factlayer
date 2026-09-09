"""Fact extraction: deterministic pre-pass + LLM Extract merged.

Pre-pass (regex/entities) guarantees a usable baseline with zero LLM cost and
feeds the LLM doc-entity hints. LLM output wins on semantics; regex wins on
verbatim numbers. Both attach verbatim quotes; verifier decides what survives.
"""
import re
from collections import Counter

from .chunk import Chunk
from .llm import available as llm_available, llm_extract
from .models import Evidence, Fact
from .normalize import canonical_period, canonical_unit, parse_number, to_base

MONEY_RE = re.compile(
    r"(₹|Rs\.?|INR|USD|\$)?\s*\(?\s*\d[\d,]*(?:\.\d+)?\s*\)?\s*"
    r"(Cr|crore?s?|lakh?s?|Mn|Bn|million|billion|%|percent)?", re.I)
PCT_RE = re.compile(r"\(?\s*\d[\d,]*(?:\.\d+)?\s*\)?\s*(%|percent)", re.I)
QTY_RE = re.compile(
    r"\(?\s*\d[\d,]*(?:\.\d+)?\s*\)?\s*(Mn|Bn|million|billion|tons?|tonnes?|shipments|employees|people|days|branches|orders)\b.{0,8}?(plus|\+)?", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")
HTML_RE = re.compile(r"</?(sup|sub|br|b|i|em|strong|span|div)[^>]*>", re.I)
MD_FMT_RE = re.compile(r"(\*\*|__|\*|_|`|#{1,6}\s?)")
FOOTNOTE_LINE_RE = re.compile(
    r"^\(?\d{1,2}\)?\s+(As per|Growth rate|Includes|Note|Source|Due to)", re.I)


def clean(s: str) -> str:
    s = HTML_RE.sub(" ", s)
    s = MD_FMT_RE.sub("", s)
    return re.sub(r"[ \t]+", " ", s).strip()
BOILER_RE = re.compile(
    r"^(contents?|what'?s inside|corporate overview|statutory reports|financial statements|"
    r"page \d+|annual report|for more details|https?://|\d+\s*$)", re.I)
ORG_RE = re.compile(r"\b([A-Z][A-Za-z&.,'’\-]+(?:\s+[A-Z][A-Za-z&.,'’\-]+){0,3})\s+(Limited|Ltd\.?|Inc\.?|Bank|Corporation|Group)\b")
ROLE_VERBS = re.compile(r"\b(is|are|was|were|became|appointed|resigned|retired|headquartered|founded|launched|acquired|merged|renamed|vision|mission)\b", re.I)


def detect_doc_entity(chunks: list[Chunk]) -> str:
    votes: Counter = Counter()
    for c in chunks[:40]:
        for m in ORG_RE.finditer(c.text):
            votes[m.group(0).strip()] += 1
    if not votes:
        return ""
    top = votes.most_common(1)[0][0]
    return re.sub(r"\s+", " ", top)


def pre_extract(chunk: Chunk, doc_entity: str) -> list[Fact]:
    facts: list[Fact] = []
    for sent in SENT_SPLIT.split(chunk.text):
        s = sent.strip().strip("| ").strip()
        if len(s) < 25 or BOILER_RE.match(clean(s)[:60]):
            continue
        if FOOTNOTE_LINE_RE.match(clean(s)[:80]):
            continue  # footnote legend lines, not facts
        numeric = _numeric_from_sentence(s, chunk, doc_entity)
        facts.extend(numeric)
        if not numeric and len(s.split()) >= 6 and (ROLE_VERBS.search(s) or ORG_RE.search(s)):
            facts.append(Fact(
                subject=_subject_of(s, doc_entity), attribute=_attr_of(s),
                value_raw=s[:160], fact_type="semantic", confidence=0.45,
                evidence=_ev(chunk, s, "text"), flags=["semantic-heuristic"]))
    return facts


def _subject_of(sent: str, doc_entity: str) -> str:
    m = ORG_RE.search(sent)
    if m:
        return m.group(0).strip()
    if doc_entity and doc_entity.split()[0].lower() in sent.lower():
        return doc_entity
    return doc_entity or "document"


def _attr_of(sent: str) -> str:
    s = clean(re.sub(r"[₹$].*", "", sent))
    words = re.findall(r"[A-Za-z][A-Za-z&\-']+", s)
    stop = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "was",
            "were", "is", "are", "with", "by", "as", "at", "from", "its", "it",
            "sup", "sub", "br"}
    kept = [w for w in words if w.lower() not in stop and len(w) > 1]
    return " ".join(kept[:6]) or clean(sent)[:60]


def _looks_like_period_fragment(raw: str, before: str) -> bool:
    """Bare short numbers glued to FY/Q tokens or years are not facts."""
    digits = re.sub(r"\D", "", raw)
    if re.search(r"(FY|Q[1-4]\s*FY)\s*$", before, re.I) and len(digits) <= 4:
        return True
    if re.fullmatch(r"\(?\d{1,2}\)?", raw.strip()) and len(digits) <= 2:
        return True  # footnote markers like (1), (2)
    return False


def _numeric_from_sentence(sent: str, chunk: Chunk, doc_entity: str) -> list[Fact]:
    out: list[Fact] = []
    seen: set[str] = set()
    for m in list(MONEY_RE.finditer(sent)) + list(PCT_RE.finditer(sent)) + list(QTY_RE.finditer(sent)):
        raw = m.group(0).strip()
        if not raw or raw in seen or not re.search(r"\d", raw):
            continue
        if len(re.sub(r"[^\w]", "", raw)) < 2:
            continue
        seen.add(raw)
        unit_raw = _tail_unit(raw)
        # extend over a closing paren: "(6.3%)" must keep its negative sign
        if raw.startswith("(") and not raw.endswith(")") and m.end() < len(sent) and sent[m.end()] == ")":
            raw += ")"
        before = sent[max(0, m.start() - 12):m.start()]
        if _looks_like_period_fragment(raw, before):
            continue
        if not unit_raw and len(re.sub(r"\D", "", raw)) <= 3:
            continue  # bare tiny number: folio, count fragment, not a fact
        v, flags = parse_number(_num_part(raw))
        unit_norm = canonical_unit(unit_raw, currency_hint=raw + " " + sent)
        base = to_base(v, unit_norm) if v is not None else None
        period, pflags = canonical_period(sent)
        window = sent[max(0, m.start() - 90):m.start()]
        window = window[window.find(" ") + 1:] if " " in window else ""  # snap to word boundary
        attr = _attr_of(window + " " + unit_raw) if window.strip() else unit_raw or "value"
        modality = "table" if "|" in sent else ("chart" if chunk.modality_hints and "chart" in chunk.modality_hints else "text")
        conf = 0.55
        if period:
            conf += 0.1
        if unit_norm not in ("qty",):
            conf += 0.1
        if modality in ("chart",):
            conf -= 0.1
            flags.append("chart-sourced")
        out.append(Fact(
            subject=_subject_of(sent, doc_entity), attribute=attr,
            value_raw=raw, value_norm=base, unit_norm=unit_norm,
            period=period, scope=_scope_of(sent), fact_type="numeric",
            confidence=round(min(conf, 0.8), 2),
            evidence=_ev(chunk, sent, modality), flags=flags + pflags))
    return out


def _tail_unit(raw: str) -> str:
    m = re.search(
        r"(Cr|crore?s?|lakh?s?|Mn|Bn|million|billion|%|percent|tons?|tonnes?|shipments|employees|people|days)"
        r"(\s+(Tons?|tonnes?|shipments?))?\+?$", raw.strip(), re.I)
    if not m:
        return ""
    return (m.group(1) + (" " + m.group(2).strip() if m.group(2) else "")).strip()


def _num_part(raw: str) -> str:
    m = re.search(r"\(?\s*[\d,]+(?:\.\d+)?\s*\)?", raw)
    return m.group(0) if m else raw


def _scope_of(sent: str) -> str:
    line = clean(sent).split("\n")[0][:160]  # same line only, capped
    quals = []
    for pat in [r"adjust(?:ed|ment)", r"standalone", r"consolidat(?:ed|ion)",
                r"pro forma", r"excluding [A-Za-z ]{0,40}", r"from services",
                r"service EBITDA", r"\btotal\b", r"\bnet\b", r"\bgross\b"]:
        m = re.search(pat, line, re.I)
        if m:
            quals.append(m.group(0).lower().strip())
    return "; ".join(quals)


def _ev(chunk: Chunk, sent: str, modality: str) -> Evidence:
    return Evidence(doc=chunk.doc, page_index=chunk.page_index,
                    page_label=chunk.page_label, quote=sent[:500], modality=modality)


def extract_chunk(chunk: Chunk, doc_entity: str) -> tuple[list[Fact], list[dict]]:
    """Returns (facts, open_questions). LLM facts merged with pre-pass."""
    pre = pre_extract(chunk, doc_entity)
    questions: list[dict] = []
    if not llm_available():
        return pre, questions
    try:
        items = llm_extract(chunk.text, chunk.doc)
    except Exception as e:
        questions.append({"kind": "llm-extract-error", "detail": f"{type(e).__name__}: {e}"})
        return pre, questions
    merged = list(pre)
    for it in items:
        try:
            f = _llm_item_to_fact(it, chunk, doc_entity)
            if f:
                merged.append(f)
        except Exception as e:
            questions.append({"kind": "llm-item-rejected",
                              "detail": f"schema error: {e}; item={str(it)[:200]}"})
    return merged, questions


def _llm_item_to_fact(it: dict, chunk: Chunk, doc_entity: str) -> Fact | None:
    quote = (it.get("quote") or "").strip()
    if not quote or quote not in chunk.text:
        raise ValueError("quote not verbatim in chunk")
    ft = it.get("fact_type", "numeric")
    vnorm, unorm, period, flags = None, "", (it.get("period") or ""), []
    if ft == "numeric":
        vnorm, unorm, per, flags = _llm_numeric(it, chunk.text)
        period = period or per
    else:
        unorm = ""
        _, pflags = canonical_period((it.get("period") or "") + " " + quote)
        flags = pflags
        if not period:
            period, _ = canonical_period(quote)
    modality = it.get("modality", "text")
    if modality == "unknown":
        modality = "table" if "|" in quote else "text"
    conf = float(it.get("confidence", 0.6) or 0.6)
    if modality in ("chart", "infographic"):
        conf = min(conf, 0.65)
        flags.append("chart-sourced")
    return Fact(
        subject=it.get("subject") or doc_entity or "document",
        attribute=it.get("attribute") or "statement",
        value_raw=it.get("value_raw") or "", value_norm=vnorm, unit_norm=unorm,
        period=period, scope=it.get("scope") or "", fact_type=ft,
        confidence=round(conf, 2),
        evidence=Evidence(doc=chunk.doc, page_index=chunk.page_index,
                          page_label=chunk.page_label, quote=quote, modality=modality),
        flags=flags)


def _llm_numeric(it: dict, context: str):
    from .normalize import normalize_fact_value
    return normalize_fact_value(it.get("value_raw") or "", it.get("unit") or "",
                                (it.get("period") or "") + " " + context[:400])
