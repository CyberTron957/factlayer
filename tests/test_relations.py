"""Unit tests for the relation engine. No API calls, no PDFs."""
from app.link import heuristic_link, link_pair, same_topic
from app.models import Evidence, Fact
from app.verify import recheck_relation


def F(fact_id, doc, attr, raw, norm, unit, period, scope="", vintage_doc="a"):
    return Fact(fact_id=fact_id, subject="Delhivery Limited", attribute=attr,
                value_raw=raw, value_norm=norm, unit_norm=unit,
                period=period, scope=scope, fact_type="numeric",
                confidence=0.8,
                evidence=Evidence(doc=doc, page_index=1, page_label="2",
                                  quote=f"{attr} {raw} {period}", modality="text"))


def test_corroborate_same_value():
    a = F("a", "d1", "revenue from services", "₹8,142 Cr", 81420000000.0, "inr-cr", "FY2024")
    b = F("b", "d2", "revenue from services", "Rs 8142 crore", 81420000000.0, "inr-cr", "FY2024")
    assert same_topic(a, b)
    assert heuristic_link(a, b, 0.02)["relation"] == "corroborates"


def test_corroborate_different_units():
    a = F("a", "d1", "revenue from services", "81,415", 81415000000.0, "inr-mn", "FY2024")
    b = F("b", "d2", "revenue from services", "₹8,142 Cr", 81420000000.0, "inr-cr", "FY2024")
    assert same_topic(a, b)  # same dimension money-inr
    assert heuristic_link(a, b, 0.02)["relation"] == "corroborates"


def test_contradict_same_context():
    a = F("a", "d1", "cross border revenue", "7,054", 70540000000.0, "inr-cr", "FY2024")
    b = F("b", "d2", "cross border revenue", "7,224", 72240000000.0, "inr-cr", "FY2024")
    assert same_topic(a, b)
    assert heuristic_link(a, b, 0.02)["relation"] == "contradicts"


def test_reconciled_by_time():
    a = F("a", "d1", "revenue from services", "₹7,223 Cr", 72230000000.0, "inr-cr", "FY2023")
    b = F("b", "d2", "revenue from services", "₹8,142 Cr", 81420000000.0, "inr-cr", "FY2024")
    r = heuristic_link(a, b, 0.02)
    assert r["relation"] == "reconciled" and r["axis"] == "time"


def test_reconciled_by_scope():
    a = F("a", "d1", "ebitda", "₹127 Cr", 1270000000.0, "inr-cr", "FY2024", scope="")
    b = F("b", "d2", "adjusted ebitda", "₹76 Cr", 760000000.0, "inr-cr", "FY2024", scope="adjusted")
    r = heuristic_link(a, b, 0.02)
    assert r["relation"] == "reconciled" and r["axis"] == "scope"


def test_superseded_by_vintage():
    # same period restated across vintages: newer disclosure wins
    a = F("a", "old", "workforce headcount", "58,000", 58000.0, "employees", "FY2024")
    b = F("b", "new", "workforce headcount", "65,000", 65000.0, "employees", "FY2024")
    rel = link_pair(a, b, 0.02, {"old": -10, "new": 10})
    assert rel.relation == "superseded-by"
    # different periods = genuine change over time, stays reconciled
    c = F("c", "old", "workforce headcount", "58,000", 58000.0, "employees", "FY2022")
    assert link_pair(c, b, 0.02, {"old": -10, "new": 10}).relation == "reconciled"


def test_recheck_corrects_false_corroboration():
    a = F("a", "d1", "revenue", "₹8,142 Cr", 81420000000.0, "inr-cr", "FY2024")
    b = F("b", "d2", "revenue", "₹7,000 Cr", 70000000000.0, "inr-cr", "FY2024")
    out = recheck_relation({"relation": "corroborates", "explanation": "x"}, a, b, 0.02)
    assert out["relation"] == "contradicts" and out["verified"]


def test_recheck_corrects_dimension_mismatch():
    a = F("a", "d1", "ebitda", "₹127 Cr", 1270000000.0, "inr-cr", "FY2024")
    b = F("b", "d2", "ebitda margin", "1.6%", 1.6, "%", "FY2024")
    assert not same_topic(a, b)  # money vs percent never blocks together
    out = recheck_relation({"relation": "contradicts", "explanation": "x"}, a, b, 0.02)
    assert out["relation"] == "reconciled" and out["axis"] == "units"


def test_paren_negative_normalization():
    from app.normalize import parse_number
    v, flags = parse_number("(404)")
    assert v == -404.0 and "paren-negative" in flags


def test_usd_not_inr():
    from app.normalize import canonical_unit, same_dimension
    assert canonical_unit("bn", "$ 282.8 billion trade deficit") == "usd-bn"
    assert canonical_unit("Cr", "₹8,142 Cr revenue") == "inr-cr"
    assert not same_dimension("usd-bn", "inr-cr")


def test_pct_in_value_beats_money_context():
    from app.normalize import normalize_fact_value
    _, unorm, _, _ = normalize_fact_value("31%", "", "revenue ₹8,142 Cr grew by 31% YoY")
    assert unorm == "%", unorm


def test_cross_dimension_veto():
    from app.link import link_pair
    from app.models import Fact, Evidence
    def mk(attr, raw, unit, period="FY2024"):
        return Fact(subject="Delhivery", attribute=attr, value_raw=raw,
                    value_norm=8142.0 if "8,142" in raw else 31.0,
                    unit_norm=unit, period=period, scope="", fact_type="numeric",
                    confidence=0.9,
                    evidence=Evidence(doc="d1", page_index=0, page_label="1", quote=raw))
    a = mk("revenue services", "8,142", "inr-cr")
    b = mk("revenue growth", "31%", "%")
    assert link_pair(a, b, 0.02, {}) is None
