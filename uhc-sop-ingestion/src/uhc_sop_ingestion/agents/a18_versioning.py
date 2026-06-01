"""SOP revision-date versioning — gate + registry agents."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from uhc_sop_ingestion.revision import (
    normalize_canonical_url,
    normalize_revision_date,
)

if TYPE_CHECKING:
    from uhc_sop_ingestion.config import PipelineConfig
    from uhc_sop_ingestion.state import PipelineState

log = logging.getLogger(__name__)

VERSION_NEW = "NEW"
VERSION_UNCHANGED = "UNCHANGED"
VERSION_REVISED = "REVISED"
VERSION_CONTENT_CHANGE = "CONTENT_CHANGE"


def _lookup_prior(cfg: "PipelineConfig", canonical_url: str) -> tuple | None:
    from uhc_sop_ingestion.agents.a11_write_postgres import _exec

    rows = _exec(cfg, """
        SELECT id, revision_date, content_hash, version_number
        FROM sop_ingestion_auditsop
        WHERE canonical_url = %s AND is_current = true
        ORDER BY version_number DESC, crawled_at DESC
        LIMIT 1
    """, (canonical_url,))
    if not rows:
        rows = _exec(cfg, """
            SELECT id, revision_date, content_hash, version_number
            FROM sop_ingestion_auditsop
            WHERE lower(rtrim(url, '/')) = lower(rtrim(%s, '/'))
              AND is_current = true
            ORDER BY version_number DESC, crawled_at DESC
            LIMIT 1
        """, (canonical_url,))
    return rows[0] if rows else None


def _apply_review_policy(state: "PipelineState", payload: dict) -> dict:
    """Revision-check re-ingests stay pending until a human activates them."""
    trigger = (state.get("trigger_source") or "manual").lower()
    version_action = payload.get("version_action", VERSION_NEW)
    if trigger == "revision_check" and version_action not in (VERSION_UNCHANGED, VERSION_NEW):
        payload = {**payload, "requires_human_review": True}
    return payload


def revision_version_gate(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """Compare parsed revision date + content hash against the current DB version."""
    url = state.get("current_url", "") or state.get("seed_url", "")
    canonical = normalize_canonical_url(url)
    meta = state.get("metadata") or {}
    revision_date = meta.get("revision_date", "")
    rev_norm = normalize_revision_date(revision_date)
    content_hash = state.get("content_hash", "")

    base = {
        "canonical_url": canonical,
        "normalized_revision_date": rev_norm,
        "prior_sop_db_id": None,
        "version_action": VERSION_NEW,
    }
    if not canonical:
        return base

    prior = _lookup_prior(cfg, canonical)
    if not prior:
        return base

    prior_id, prior_rev, prior_hash, _prior_ver = prior
    base["prior_sop_db_id"] = prior_id

    if rev_norm or prior_rev:
        same_rev = (
            normalize_revision_date(prior_rev or "") == rev_norm
            if rev_norm
            else (prior_rev or "").strip().lower() == (revision_date or "").strip().lower()
        )
        if same_rev and content_hash == prior_hash:
            log.info(
                "revision_version_gate: unchanged  url=%s  rev=%s  hash=%s",
                canonical, rev_norm or revision_date, content_hash,
            )
            return {
                **base,
                "version_action": VERSION_UNCHANGED,
                "is_duplicate": True,
            }
        if same_rev and content_hash != prior_hash:
            return _apply_review_policy(state, {**base, "version_action": VERSION_CONTENT_CHANGE})
        if not same_rev:
            return _apply_review_policy(state, {**base, "version_action": VERSION_REVISED})

    if content_hash == prior_hash:
        return {**base, "version_action": VERSION_UNCHANGED, "is_duplicate": True}
    return _apply_review_policy(state, {**base, "version_action": VERSION_CONTENT_CHANGE})


def pg_version_registry(state: "PipelineState", cfg: "PipelineConfig") -> dict:
    """After PG writers finish, register version chain + compute diff."""
    sop_id = state.get("sop_db_id")
    if not sop_id:
        return {}

    version_action = state.get("version_action") or VERSION_NEW
    if version_action == VERSION_UNCHANGED:
        return {"version_registered": False, "version_action": version_action}

    try:
        from sop_ingestion.services.versioning import register_sop_version

        diff = register_sop_version(
            int(sop_id),
            prior_sop_id=state.get("prior_sop_db_id"),
            version_action=version_action,
            canonical_url=state.get("canonical_url", ""),
            revision_date=(state.get("metadata") or {}).get("revision_date", ""),
            auto_activate=not bool(state.get("requires_human_review")),
        )
    except Exception as exc:
        log.exception("pg_version_registry failed for sop_db_id=%s", sop_id)
        return {"errors": [{"agent": "pg_version_registry", "msg": str(exc)}]}

    out = {
        "version_registered": True,
        "version_action": version_action,
        "postgres_doc_id": sop_id,
        "requires_human_review": bool(state.get("requires_human_review")),
    }
    if diff:
        out["version_diff_id"] = diff.id
        out["version_diff_summary"] = diff.summary
    return out
