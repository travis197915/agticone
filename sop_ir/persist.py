"""The one shared Django ORM write gate for a canonical SOP IR.

``persist_ir`` is the single place that turns a validated :class:`SopIR` into
routing-complete ``AuditStep`` / ``AuditDecision`` rows. Both ingestion doors
flow through it:

  * the YAML importer (``import_sop_yaml``) builds a ``SopIR`` then calls here
  * the HTML/PDF pipeline emits ``state["sop_ir"]`` which ``pipeline_runner``
    hands here after ``pipeline.run()`` returns (Django ORM context)

The recursive planning + writing logic lived in ``import_sop_yaml.py``; it is
moved here verbatim (behaviour-preserving for the YAML door) so HTML/PDF rows
become exactly as rich as hand-authored rows. All heuristics come from
:mod:`sop_ir.normalize` — the divergent duplicate ``_classify_decision`` in
``a11_write_postgres.py`` is retired.

This module is the ONLY one in the package that imports Django.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Optional

from django.db import transaction

from sop_ingestion.models import (
    AuditDecision, AuditReference, AuditSop, AuditStep, SopIRDocument,
)

from .plan import plan_ir
from .schema import SopIR

log = logging.getLogger(__name__)


# ── writing — ported from import_sop_yaml._write_* ────────────────────────────
def _write_step(sop: AuditSop, p: dict, stats: dict) -> None:
    step = AuditStep.objects.create(
        sop=sop,
        step_number=p["step_number"],
        question=p["question"],
        intro_text=p["intro_text"],
        is_terminal=p["is_terminal"],
        terminal_action=p["terminal_action"],
        is_out_of_scope=p["is_out_of_scope"],
        yaml_rule_id=p["rule_id"],
    )
    stats["steps"] += 1
    if p["is_out_of_scope"]:
        stats["oos"] += 1

    # Step-global pre-order ordinal for row_index keeps the
    # `step:<sop>:<step_number>:<row_index>` rule_key UNIQUE within a step even
    # though sibling indexes restart at 0 per parent.
    counter = [0]
    for child in p["children"]:
        _write_decision(step, child, parent=None, stats=stats, counter=counter)

    for txt in p["references"]:
        AuditReference.objects.create(
            sop=sop, step=step, ref_text=txt, ref_url="",
            ref_type="YAML_RULE", is_resolved=False,
        )
        stats["refs"] += 1
    for url in p["urls"]:
        AuditReference.objects.create(
            sop=sop, step=step, ref_text="", ref_url=url,
            ref_type="YAML_RULE", is_resolved=True,
        )
        stats["refs"] += 1


def _write_decision(step: AuditStep, d: dict, parent, stats: dict, counter: list) -> None:
    codes = d["codes"]
    all_codes = list(dict.fromkeys(
        codes["eob"] + codes["ex"] + codes["denial"] + codes["sysact"]
    ))
    row_index = counter[0]
    counter[0] += 1
    dec = AuditDecision.objects.create(
        step=step,
        parent=parent,
        depth=d["depth"],
        subrule_id=d.get("subrule_id", ""),
        table_name=d.get("table_name", ""),
        aggregation=d["aggregation"],
        applicable_when=d.get("applicable_when", ""),
        row_index=row_index,
        condition_if=d["condition_if"],
        condition_and=d["condition_and"],
        action_text=d["action_text"],
        output_text=d["output_text"],
        decision_type=d["decision_type"],
        tooling_allowed=d["tooling_allowed"],
        is_out_of_scope=d["is_out_of_scope"],
        goto_step=d["goto_step"],
        is_final=d["is_final"],
        eob_codes=codes["eob"],
        ex_codes=codes["ex"],
        denial_codes=codes["denial"],
        system_actions=codes["sysact"],
        all_codes=all_codes,
    )
    stats["decisions"] += 1
    if d["is_out_of_scope"]:
        stats["oos"] += 1

    for c in d["children"]:
        _write_decision(step, c, parent=dec, stats=stats, counter=counter)


def _wipe(sop: AuditSop, preserve_step_numbers: Optional[set] = None) -> None:
    # Deleting steps cascades their AuditDecision rows (incl. nested children).
    qs = AuditStep.objects.filter(sop=sop)
    if preserve_step_numbers:
        # Keep e.g. the synthetic "Step 0 — Pre-Step Exceptions" written by the
        # pipeline's pg_precondition_writer; the IR projection owns steps >= 1.
        qs = qs.exclude(step_number__in=preserve_step_numbers)
    qs.delete()
    # Only remove references that carry no step link AND were authored by us.
    AuditReference.objects.filter(sop=sop, ref_type="YAML_RULE").delete()


# ── canonical IR document (control-plane head + Mongo archive) ───────────────
def _mongo_db():
    """Django-side Mongo handle (mirrors uhc_sop_ingestion.config.get_mongo).

    Returns the database handle or None when Mongo is not configured/reachable
    — the relational write is authoritative, so a missing Mongo never blocks a
    persist (it just skips the lossless archive)."""
    try:
        from pymongo import MongoClient

        from sop_backend.db_config import is_prod, mongo_uri_from_env

        if not (os.environ.get("MONGO_HOST") or (is_prod() and os.environ.get("MONGO_URI"))):
            return None
        database = os.environ.get("MONGO_DATABASE")
        client = MongoClient(mongo_uri_from_env(), serverSelectionTimeoutMS=10000)
        return client[database]
    except Exception as exc:
        log.warning("persist_ir: Mongo unavailable (%s) — skipping IR archive", exc)
        return None


def _content_hash(ir_json: dict) -> str:
    blob = json.dumps(ir_json, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _write_ir_document(sop: AuditSop, ir: SopIR, *, job, stats: dict,
                       source: str, validation: Optional[dict]) -> SopIRDocument:
    """Persist the authoritative IR record (Postgres) + lossless archive (Mongo)."""
    ir_json = ir.model_dump(mode="json")
    chash = _content_hash(ir_json)

    last = (SopIRDocument.objects.filter(sop=sop)
            .order_by("-ir_version").values_list("ir_version", flat=True).first())
    version = (last or 0) + 1

    status = "UNCHECKED"
    errors: list = []
    if validation is not None:
        ok = validation.get("ok")
        errors = validation.get("errors") or []
        status = "OK" if ok else ("FLAGGED" if ok is False else "UNCHECKED")

    mongo_ref = f"{sop.id}:{version}"
    doc = SopIRDocument.objects.create(
        sop=sop, job=job, ir_version=version,
        content_hash=chash, mongo_ref=mongo_ref, source=source or "",
        validation_status=status, validation_errors=errors,
        rule_count=len(ir.rules),
        step_count=stats.get("steps", 0),
        decision_count=stats.get("decisions", 0),
    )

    db = _mongo_db()
    if db is not None:
        try:
            db["sop_ir_documents"].replace_one(
                {"_id": mongo_ref},
                {
                    "_id": mongo_ref,
                    "sop_id": sop.id,
                    "job_id": str(getattr(job, "job_id", "") or ""),
                    "ir_version": version,
                    "content_hash": chash,
                    "source": source or "",
                    "validation_status": status,
                    "validation_errors": errors,
                    "ir": ir_json,
                },
                upsert=True,
            )
        except Exception as exc:
            log.warning("persist_ir: Mongo archive write failed (%s)", exc)

    return doc


# ── public entry point ────────────────────────────────────────────────────────
def persist_ir(sop: AuditSop, ir: SopIR, *, job=None, wipe: bool = True,
               plan: Optional[list[dict]] = None, source: str = "",
               validation: Optional[dict] = None,
               write_ir_document: bool = True,
               preserve_step_numbers: Optional[set] = None) -> dict:
    """Write a validated ``SopIR`` into the relational audit schema.

    Single shared gate for both ingestion doors. Idempotent: by default it
    wipes the SOP's steps/decisions/references and rebuilds an identical tree
    keyed on the IR rule ids. It also records the authoritative IR document
    (Postgres ``SopIRDocument`` + lossless Mongo archive) unless disabled.

    Args:
        source:     provenance tag stored on the IR document
                    (``yaml_import`` / ``deterministic_draft`` / ``llm_routing_enriched``).
        validation: optional ``{"ok": bool|None, "errors": [...]}`` from the
                    IR checker; recorded as the document's validation status.

    Returns a stats dict ``{steps, decisions, refs, oos, ir_version}``.
    """
    if plan is None:
        plan = plan_ir(ir)

    stats = {"steps": 0, "decisions": 0, "refs": 0, "oos": 0}
    with transaction.atomic():
        if wipe:
            _wipe(sop, preserve_step_numbers=preserve_step_numbers)
        for p in plan:
            _write_step(sop, p, stats)
        sop.step_count = stats["steps"]
        sop.decision_count = stats["decisions"]
        sop.save(update_fields=["step_count", "decision_count", "updated_at"])

        if write_ir_document:
            doc = _write_ir_document(sop, ir, job=job, stats=stats,
                                     source=source, validation=validation)
            stats["ir_version"] = doc.ir_version

    return stats
