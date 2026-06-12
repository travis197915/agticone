"""Pydantic contract for the canonical SOP Intermediate Representation.

The IR mirrors the hand-authored ``yaml/*.yaml`` shape (``sop_metadata`` +
``sop_rules`` with recursive ``subrules``) because that shape is what the
relational writer (`sop_ir.persist.persist_ir`) already knows how to turn into
routing-complete ``AuditStep`` / ``AuditDecision`` rows. The *normalization*
heuristics (disposition classification, code extraction, goto inference,
aggregation) live in :mod:`sop_ir.normalize` and run inside ``persist_ir`` — the
IR itself stays close to the source so it round-trips losslessly to Mongo.

No Django import here: the pipeline package consumes ``SopIR.model_json_schema()``
as the LLM output contract, and that must work without a configured Django app.

The enums below intentionally MIRROR ``AuditDecision.DECISION_CHOICES`` /
``AGGREGATION_CHOICES``. ``sop_ir/tests/test_roundtrip.py`` asserts they never
drift from the model definitions.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .normalize import as_list, as_text


# ── enums (mirror sop_ingestion.models.AuditDecision choices) ─────────────────
class DecisionType(str, Enum):
    DENY = "DENY"
    ALLOW = "ALLOW"
    BYPASS = "BYPASS"
    PEND = "PEND"
    REFER = "REFER"
    SYSTEM = "SYSTEM"
    STOP = "STOP"
    WAIVE = "WAIVE"
    CONDITIONAL = "CONDITIONAL"


class Aggregation(str, Enum):
    FIRST_MATCH = "FIRST_MATCH"
    XOR_ONE = "XOR_ONE"
    APPLICABLE_ONLY = "APPLICABLE_ONLY"
    ALWAYS_MET = "ALWAYS_MET"
    ANY = "ANY"
    LEAF = "LEAF"


class NavOp(str, Enum):
    """Structured navigation verb. Mirrors the ``{"navigation": {...}}`` hint
    that already appears verbatim in several YAML ``output`` blocks (e.g.
    Timely_Filing RULE-001 -> ``{"op": "goto", "step_number": 17}``)."""
    GOTO = "goto"
    STOP = "stop"
    NEXT = "next"
    PROCEED = "proceed"


class Navigation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    op: NavOp = NavOp.NEXT
    step_number: Optional[int] = Field(
        default=None,
        description="Target step_number when op == goto. Ignored otherwise.",
    )


# ── recursive rule tree ───────────────────────────────────────────────────────
class Subrule(BaseModel):
    """One If/Then decision row (recursive: a subrule may have subrules).

    Maps 1:1 to an ``AuditDecision`` row after ``persist_ir`` planning."""
    model_config = ConfigDict(extra="ignore")

    subrule_id: str = ""
    table_name: str = ""
    description: str = ""
    conditions: List[str] = Field(default_factory=list)
    actions: List[str] = Field(default_factory=list)
    output: str = ""
    # Free-text guard ("Provider is individual"). When present on a direct
    # child, persist_ir forces the parent's children to APPLICABLE_ONLY.
    applicable_when: str = ""
    tooling_allowed: bool = True
    urls: List[str] = Field(default_factory=list)
    # Optional structured routing hint; persist_ir falls back to parsing the
    # action/output text via normalize.extract_goto when this is absent.
    navigation: Optional[Navigation] = None
    subrules: List["Subrule"] = Field(default_factory=list)

    @field_validator("subrule_id", "table_name", "description", "output",
                     "applicable_when", mode="before")
    @classmethod
    def _coerce_text(cls, v: Any) -> str:
        return as_text(v)

    @field_validator("conditions", "actions", "urls", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> List[str]:
        return as_list(v)


class RuleNode(BaseModel):
    """One top-level SOP rule == one ``AuditStep`` (plus its decision subtree)."""
    model_config = ConfigDict(extra="ignore")

    rule_id: str = ""
    step_number: Optional[int] = None
    description: str = ""          # becomes AuditStep.question
    section: str = ""
    conditions: List[str] = Field(default_factory=list)
    actions: List[str] = Field(default_factory=list)
    output: str = ""
    references: List[str] = Field(default_factory=list)
    urls: List[str] = Field(default_factory=list)
    tooling_allowed: bool = True
    # Explicit child-group routing (e.g. "applicable_only", "any_clean").
    aggregation_rule: Optional[str] = None
    navigation: Optional[Navigation] = None
    subrules: List[Subrule] = Field(default_factory=list)

    @field_validator("rule_id", "description", "section", "output", mode="before")
    @classmethod
    def _coerce_text(cls, v: Any) -> str:
        return as_text(v)

    @field_validator("conditions", "actions", "references", "urls", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> List[str]:
        return as_list(v)

    @field_validator("step_number", mode="before")
    @classmethod
    def _coerce_step_number(cls, v: Any) -> Optional[int]:
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None


class SopMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")  # keep agent_info & friends verbatim

    document_title: str = ""
    document_version: str = ""
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    processing_mode: str = ""
    provider: str = ""
    source_file: str = ""
    total_rules: Optional[int] = None

    @field_validator("document_title", "document_version", "processing_mode",
                     "provider", "source_file", mode="before")
    @classmethod
    def _coerce_text(cls, v: Any) -> str:
        return as_text(v)


class SopIR(BaseModel):
    """The canonical SOP IR — the one shape both ingestion doors must produce."""
    model_config = ConfigDict(extra="ignore")

    metadata: SopMetadata = Field(default_factory=SopMetadata)
    rules: List[RuleNode] = Field(default_factory=list)

    @classmethod
    def from_yaml_doc(cls, doc: dict) -> "SopIR":
        """Build a ``SopIR`` from a parsed ``sop_metadata`` + ``sop_rules`` doc."""
        if not isinstance(doc, dict):
            raise ValueError("SOP YAML root must be a mapping.")
        return cls(
            metadata=doc.get("sop_metadata") or {},
            rules=doc.get("sop_rules") or [],
        )

    def step_numbers(self) -> List[int]:
        """Declared step numbers, falling back to source order when missing."""
        nums: List[int] = []
        for i, r in enumerate(self.rules):
            nums.append(r.step_number if r.step_number is not None else i)
        return nums


Subrule.model_rebuild()
