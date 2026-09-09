"""Pydantic schemas: the fact/relation contract. No dataset-specific fields."""
from typing import List, Literal, Optional
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    doc: str                                  # filename
    page_index: int                           # 0-based index in the PDF file
    page_label: Optional[str] = None          # printed number (excerpts jump)
    quote: str                                # verbatim substring of parsed page
    crop: Optional[str] = None                # path to rendered page-crop PNG
    modality: Literal["text", "table", "chart", "infographic", "unknown"] = "unknown"


class Fact(BaseModel):
    fact_id: str = ""
    subject: str
    attribute: str
    value_raw: str = ""
    value_norm: Optional[float] = None        # comparable number (base units) or None
    unit_norm: str = ""
    period: str = ""                          # canonical, e.g. FY2024, Q4-FY2024
    scope: str = ""                           # e.g. "adjusted", "standalone" (raw qualifiers kept)
    fact_type: Literal["numeric", "semantic"] = "numeric"
    confidence: float = 0.5
    evidence: Evidence
    flags: List[str] = Field(default_factory=list)  # quality warnings


class Relation(BaseModel):
    relation: Literal["corroborates", "contradicts", "reconciled", "superseded-by"]
    axis: Optional[Literal["time", "scope", "units", "vintage"]] = None
    fact_ids: List[str]
    explanation: str
    confidence: float = 0.5
    verified: bool = False                    # passed the numeric re-check


class OpenQuestion(BaseModel):
    kind: str                                 # e.g. "verifier-rejection", "chart-only", "ambiguous-period"
    detail: str
    fact_ids: List[str] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)
