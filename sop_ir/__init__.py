"""Canonical SOP Intermediate Representation (IR).

This package is the single source of truth for the *shape* a Standard
Operating Procedure must take before it can be loaded into the relational
audit schema and executed by the engine.

It exists so that **both** ingestion doors produce identical, routing-complete
rows:

  * hand-authored ``yaml/*.yaml``           (``import_sop_yaml`` management cmd)
  * HTML / PDF / DOCX agentic ingestion     (``uhc-sop-ingestion`` pipeline)

Layers (kept dependency-light on purpose):

  * :mod:`sop_ir.normalize`  -- pure text/heuristic helpers (no Django, no pydantic)
  * :mod:`sop_ir.schema`     -- pydantic IR contract (no Django import)
  * :mod:`sop_ir.validate`   -- routing-invariant checks over a ``SopIR``
  * :mod:`sop_ir.persist`    -- the one shared Django ORM write gate (``persist_ir``)

``schema`` and ``validate`` are importable from the pipeline package (no Django
required); only ``persist`` touches the ORM.
"""
from __future__ import annotations

from .schema import (
    Aggregation,
    DecisionType,
    NavOp,
    Navigation,
    RuleNode,
    SopIR,
    SopMetadata,
    Subrule,
)
from .validate import validate_ir

__all__ = [
    "Aggregation",
    "DecisionType",
    "NavOp",
    "Navigation",
    "RuleNode",
    "SopIR",
    "SopMetadata",
    "Subrule",
    "validate_ir",
]
