"""Slot-matching and content-change detection for Workbench versioning.

Independent of ``AuditSop.version_number``/``supersedes`` and
``sop_ingestion.services.ingestion_review`` — that module gates whether a
re-ingest is even allowed to reach auto-build (see
``sop_ingestion/pipeline_runner.py:_maybe_raise_sop_review``), keyed off
``NodeRuleBinding``-bound ``document_id``/title. This module answers a
narrower, independent question once auto-build IS allowed to run: which
existing ``Workbench`` (if any) does a freshly-ingested ``AuditSop`` belong to,
and has its content actually changed since that ``Workbench`` was built. The
two heuristics are deliberately not shared — this one only ever looks at
``Workbench.config``, never at ``NodeRuleBinding`` rows or
``AuditSop.version_number``/``supersedes``.
"""
from __future__ import annotations

from builder.models import Workbench


def _norm_title(title: str) -> str:
    return " ".join((title or "").split()).casefold()


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def find_matching_workbench(workflow, sop) -> Workbench | None:
    """The current Workbench slot ``sop`` belongs to within ``workflow``, if any.

    Match order, scoped to this workflow's *current* Workbenches:
      1. ``Workbench.config['source_url']`` (or ``'yaml_ref'``) equals
         ``sop.canonical_url`` or ``sop.url``.
      2. Fallback: normalized-title equality against
         ``Workbench.config['sop_title']``.

    Returns ``None`` when nothing matches — ``sop`` is a brand-new slot.
    """
    candidates = list(
        Workbench.objects.filter(work_area__workflow=workflow, is_current=True)
    )
    if not candidates:
        return None

    sop_urls = {
        u for u in (
            _norm_url(getattr(sop, "canonical_url", "")),
            _norm_url(getattr(sop, "url", "")),
        ) if u
    }
    if sop_urls:
        for wb in candidates:
            cfg = wb.config or {}
            wb_urls = {
                u for u in (
                    _norm_url(cfg.get("source_url", "")),
                    _norm_url(cfg.get("yaml_ref", "")),
                ) if u
            }
            if sop_urls & wb_urls:
                return wb

    title = _norm_title(getattr(sop, "title", ""))
    if title:
        for wb in candidates:
            cfg = wb.config or {}
            if _norm_title(cfg.get("sop_title", "")) == title:
                return wb

    return None


def content_unchanged(workbench: Workbench, sop) -> bool:
    """True if ``sop.content_hash`` matches the hash ``workbench`` was last
    built from.

    A missing stored hash (a pre-feature Workbench that predates this stamp)
    is treated as changed, not unchanged, so it gets backfilled by one version
    bump rather than silently assumed current.
    """
    stored = (workbench.config or {}).get("content_hash")
    if not stored:
        return False
    return stored == (getattr(sop, "content_hash", "") or "")
