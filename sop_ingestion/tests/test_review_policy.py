"""Unit tests for human-review gating on revision-check re-ingests."""
from uhc_sop_ingestion.agents.a18_versioning import (
    VERSION_CONTENT_CHANGE,
    VERSION_NEW,
    VERSION_REVISED,
    VERSION_UNCHANGED,
    _apply_review_policy,
)


def test_revision_check_marks_revised_version_for_review():
    state = {"trigger_source": "revision_check"}
    payload = {"version_action": VERSION_REVISED}
    out = _apply_review_policy(state, payload)
    assert out["requires_human_review"] is True


def test_manual_ingest_does_not_require_review():
    state = {"trigger_source": "manual"}
    payload = {"version_action": VERSION_REVISED}
    out = _apply_review_policy(state, payload)
    assert "requires_human_review" not in out


def test_revision_check_first_ingest_stays_auto_active():
    state = {"trigger_source": "revision_check"}
    payload = {"version_action": VERSION_NEW}
    out = _apply_review_policy(state, payload)
    assert "requires_human_review" not in out


def test_unchanged_never_requires_review():
    state = {"trigger_source": "revision_check"}
    payload = {"version_action": VERSION_UNCHANGED}
    out = _apply_review_policy(state, payload)
    assert "requires_human_review" not in out


def test_content_change_from_revision_check_requires_review():
    state = {"trigger_source": "revision_check"}
    payload = {"version_action": VERSION_CONTENT_CHANGE}
    out = _apply_review_policy(state, payload)
    assert out["requires_human_review"] is True
