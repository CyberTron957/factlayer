"""Generic normalizers: numbers, units, periods. No dataset-specific rules.

Handles Indian + international formats: 8,142 / 81,415 / (404) = -404 /
Rs 1.4 Mn / Rs 29 Cr / 12.7% / FY24 / Q4 FY24 / FY2023-24 / year ended Mar 2024.
"""
import re

# canonical unit -> multiplier to a base (per-dimension; cross-dimension never compares)
UNIT_MULT = {
    # currency (base: INR)
    "inr": 1, "rs": 1, "rupee": 1, "rupees": 1,
    "inr-cr": 1e7, "cr": 1e7, "crore": 1e7, "crores": 1e7,
    "inr-lakh": 1e5, "lakh": 1e5, "lakhs": 1e5,
    "inr-mn": 1e6, "mn-inr": 1e6, "million-inr": 1e6,
    "inr-bn": 1e9, "bn-inr": 1e9,
    "usd": 1, "usd-mn": 1e6, "usd-bn": 1e9,
    # counts / masses (base: stated unit family)
    "qty": 1, "mn": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9,
    "k": 1e3, "thousand": 1e3, "tons": 1, "tonnes": 1, "mn-tons": 1e6,
    "shipments": 1, "mn-shipments": 1e6, "bn-shipments": 1e9,
    "people": 1, "employees": 1, "days": 1, "%": 1, "percent": 1,
}
UNIT_DIM = {  # comparability families
    "inr": "money-inr", "rs": "money-inr", "rupee": "money-inr", "rupees": "money-inr",
    "inr-cr": "money-inr", "cr": "money-inr", "crore": "money-inr", "crores": "money-inr",
    "inr-lakh": "money-inr", "lakh": "money-inr", "lakhs": "money-inr",
    "inr-mn": "money-inr", "mn-inr": "money-inr", "million-inr": "money-inr",
    "inr-bn": "money-inr", "bn-inr": "money-inr",
    "usd": "money-usd", "usd-mn": "money-usd", "usd-bn": "money-usd",
    "qty": "qty", "mn": "qty-scale", "million": "qty-scale",
    "bn": "qty-scale", "billion": "qty-scale",
    "k": "qty-k", "thousand": "qty-k", "tons": "mass", "tonnes": "mass", "mn-tons": "mass",
    "shipments": "count", "mn-shipments": "count", "bn-shipments": "count",
    "people": "people", "employees": "people", "days": "days",
    "%": "pct", "percent": "pct",
}

NUM_RE = re.compile(r"\(?\s*[\d,]+(?:\.\d+)?\s*\)?")


def parse_number(raw: str) -> tuple[float | None, list[str]]:
    """Returns (value, flags). Parentheses => negative (accounting convention)."""
    flags: list[str] = []
    s = raw.strip()
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1].strip()
        flags.append("paren-negative")
    s = s.replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None, flags + ["unparseable-number"]
    v = float(m.group())
    if neg:
        v = -abs(v)
    return v, flags


def canonical_unit(unit_raw: str, currency_hint: str = "") -> str:
    u = unit_raw.strip().lower().replace("₹", "").replace("rs.", "rs").strip(" .")
    hint = currency_hint.lower()
    usd = "$" in hint or "usd" in hint or "dollar" in hint
    money = usd or any(k in hint for k in ["₹", "rs", "inr"])
    count_nearby = any(k in hint for k in
                       ["shipment", "ton", "employee", "people", "branch", "order", "parcel"])
    scale = "cr" if "cr" in hint or "crore" in hint else (
        "lakh" if "lakh" in hint else (
        "mn" if re.search(r"\bmn\b|\bmil\b|million", hint) else (
        "bn" if re.search(r"\bbn\b|billion", hint) else "")))
    table = {
        "cr": "inr-cr", "crs": "inr-cr", "crore": "cr", "crores": "cr",
        "lakh": "inr-lakh", "lakhs": "inr-lakh", "%": "%", "percent": "percent",
        "mn": "mn", "million": "million", "bn": "bn", "billion": "billion",
        "tons": "tons", "tonnes": "tons", "t": "tons",
        "shipments": "shipments", "employees": "employees", "people": "people",
        "days": "days", "k": "k", "thousand": "thousand",
        "million-inr": "inr-mn", "inr million": "inr-mn", "₹ million": "inr-mn",
    }
    if not u:  # bare number: inherit scale from nearby money context
        if money and scale == "cr":
            return "inr-cr"
        if money and scale == "lakh":
            return "inr-lakh"
        if money and scale in ("mn", "bn"):
            fam = "usd" if usd else "inr"
            return f"{fam}-mn" if scale == "mn" else f"{fam}-bn"
        if usd:
            return "usd"
        return "inr" if money else "qty"
    if u not in table:
        # compound scale+noun units ("mn tons", "million shipments")
        m2 = re.match(r"(mn|million|bn|billion)\s+(tons?|tonnes?|shipments?)$", u)
        if m2:
            scale_w, noun_w = m2.groups()
            if noun_w.startswith("ship"):
                return "mn-shipments" if scale_w in ("mn", "million") else "bn-shipments"
            return "mn-tons"
        return u  # unknown units stay raw (never silently forced)
    cu = table[u]
    noun = ("shipments" if "shipment" in hint else
            "tons" if re.search(r"\btons?\b|\btonnes?\b", hint) else "")
    # count nouns bind to their scale: "740 Mn" (shipments) -> mn-shipments
    if noun and cu in ("mn", "million"):
        return "mn-shipments" if noun == "shipments" else "mn-tons"
    if noun and cu in ("bn", "billion"):
        return "bn-shipments" if noun == "shipments" else "bn"
    # attach money family to bare scale words ONLY for money context,
    # never when a count noun (shipments/tons/...) is adjacent
    if cu in ("mn", "million") and money and not count_nearby:
        return "usd-mn" if usd else "inr-mn"
    if cu in ("bn", "billion") and money and not count_nearby:
        return "usd-bn" if usd else "inr-bn"
    return cu


def to_base(value: float, unit_norm: str) -> float | None:
    mult = UNIT_MULT.get(unit_norm)
    return value * mult if mult is not None else None


def same_dimension(u1: str, u2: str) -> bool:
    return UNIT_DIM.get(u1, u1) == UNIT_DIM.get(u2, u2)


FY_RE = re.compile(r"\bFY\s?(\d{2,4})\b", re.I)
FISCAL_RE = re.compile(r"\bFiscal\s?(?:year\s?ended[^.]{0,30}?)?(20\d{2})\b", re.I)
NINEMONTH_RE = re.compile(r"nine months?(?: period)? ended[^.]{0,40}?(20\d{2})", re.I)
Q_RE = re.compile(r"\bQ([1-4])\s*FY\s?(\d{2,4})\b", re.I)
FYRANGE_RE = re.compile(r"\bFY\s?(\d{4})\s*[-–]\s*(\d{2,4})\b", re.I)
YEAR_ENDED_RE = re.compile(r"year ended[^.]{0,40}?(\d{1,2}\s+\w+\s+)?(20\d{2})", re.I)
CAL_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def canonical_period(text: str) -> tuple[str, list[str]]:
    """Extract canonical period from surrounding text. Returns (period, flags)."""
    flags: list[str] = []
    q = Q_RE.search(text)
    if q:
        return f"Q{q.group(1)}-FY{_fy4(q.group(2))}", flags
    r = FYRANGE_RE.search(text)
    if r:
        return f"FY{_fy4(r.group(2))}", flags + ["fy-range"]
    f = FY_RE.search(text)
    if f:
        return f"FY{_fy4(f.group(1))}", flags
    g = FISCAL_RE.search(text)
    if g:
        return f"FY{g.group(1)}", flags + ["fiscal-year"]
    nm = NINEMONTH_RE.search(text)
    if nm:
        return f"9M-{nm.group(1)}", flags + ["nine-months"]
    y = YEAR_ENDED_RE.search(text)
    if y:
        return f"FY{y.group(2)[-2:]}-ish", flags + ["year-ended-approx"]
    return "", flags + ["no-period"]


def _fy4(yy: str) -> str:
    yy = yy.strip()
    if len(yy) == 4:
        return yy
    y = int(yy)
    return f"20{y:02d}" if y < 50 else f"19{y:02d}"


def normalize_fact_value(value_raw: str, unit_raw: str,
                         context: str) -> tuple[float | None, str, str, list[str]]:
    """Full numeric normalization. Returns (value_base, unit_norm, period, flags)."""
    v, flags = parse_number(value_raw)
    u = (unit_raw or "").strip().lower()
    if "%" in value_raw and u not in ("%", "percent"):
        u = "%"  # explicit % in the value beats any nearby context symbols
        flags = flags + ["pct-forced"]
    unit_norm = canonical_unit(u, currency_hint=value_raw + " " + context)
    period, pflags = canonical_period(context)
    base = to_base(v, unit_norm) if v is not None else None
    return base, unit_norm, period, flags + pflags
