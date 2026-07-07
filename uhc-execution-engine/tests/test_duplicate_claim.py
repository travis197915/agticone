"""Unit tests for duplicate-claim skip helpers (no database)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from uhc_execution_engine.duplicate_claim import (
    SKIP_REASON_BATCH_DUPLICATE,
    SKIP_REASON_PRIOR_CLEAN,
    _prior_fields,
    _skip_message,
    find_prior_clean_run,
    record_skipped_claim,
    skip_metadata,
)


def test_prior_fields_from_dict():
    fields = _prior_fields({
        "run_id": "run-1",
        "final_decision_type": "APPROVE",
        "applied_codes": ["X"],
        "narrative": "ok",
        "claim_lob": {"label": "PPO"},
    })
    assert fields["reused_from_run_id"] == "run-1"
    assert fields["final_decision_type"] == "APPROVE"
    assert fields["applied_codes"] == ["X"]


def test_prior_fields_from_run_object():
    prior = mock.Mock()
    prior.id = "abc"
    prior.final_decision_type = "DENY"
    prior.applied_codes = ["D1"]
    prior.narrative = "defect"
    prior.claim_lob = {}
    fields = _prior_fields(prior)
    assert fields["reused_from_run_id"] == "abc"
    assert fields["final_decision_type"] == "DENY"


def test_skip_metadata_round_trip():
    payload = {
        "_skip_reason": SKIP_REASON_BATCH_DUPLICATE,
        "_reused_from_run_id": "run-9",
        "paid_dt": "2026-01-01",
    }
    assert skip_metadata(payload) == {
        "skip_reason": SKIP_REASON_BATCH_DUPLICATE,
        "reused_from_run_id": "run-9",
    }


def test_find_prior_clean_run_returns_newest_clean():
    defect_run = mock.Mock(status="COMPLETED")
    clean_run = mock.Mock(status="COMPLETED")
    mock_run_model = mock.Mock()
    qs = mock.Mock()
    qs.exclude.return_value = qs
    qs.select_related.return_value = qs
    qs.order_by.return_value = qs
    qs.__getitem__ = mock.Mock(return_value=[defect_run, clean_run])
    mock_run_model.objects.filter.return_value = qs
    mock_serializers = mock.Mock()
    mock_serializers.claim_audit_status.side_effect = ["DEFECT", "CLEAN"]
    modules = {
        "execution_app.models": mock.Mock(RuleExecutionRun=mock_run_model),
        "execution_app.serializers": mock_serializers,
        "execution_app.trace_builder": mock.Mock(CLEAN="CLEAN"),
    }
    with mock.patch.dict(sys.modules, modules):
        found = find_prior_clean_run(
            claim_id="CLM-1",
            workflow_id="wf-1",
            exclude_batch_id="batch-2",
        )
    assert found is clean_run


def test_find_prior_clean_run_returns_none_when_not_clean():
    mock_run_model = mock.Mock()
    qs = mock.Mock()
    qs.exclude.return_value = qs
    qs.select_related.return_value = qs
    qs.order_by.return_value = qs
    qs.__getitem__ = mock.Mock(return_value=[mock.Mock(status="COMPLETED")])
    mock_run_model.objects.filter.return_value = qs
    mock_serializers = mock.Mock()
    mock_serializers.claim_audit_status.return_value = "DEFECT"
    modules = {
        "execution_app.models": mock.Mock(RuleExecutionRun=mock_run_model),
        "execution_app.serializers": mock_serializers,
        "execution_app.trace_builder": mock.Mock(CLEAN="CLEAN"),
    }
    with mock.patch.dict(sys.modules, modules):
        found = find_prior_clean_run(
            claim_id="CLM-2",
            workflow_id="wf-1",
            exclude_batch_id=None,
        )
    assert found is None


def test_record_skipped_claim_persists_and_returns_payload():
    mock_run_model = mock.Mock()
    modules = {
        "execution_app.models": mock.Mock(RuleExecutionRun=mock_run_model),
    }
    prior = {
        "run_id": "prior-1",
        "final_decision_type": "APPROVE",
        "applied_codes": ["A1"],
        "narrative": "clean",
        "claim_lob": {},
    }
    with mock.patch.dict(sys.modules, modules), mock.patch(
        "uhc_execution_engine.duplicate_claim.timezone.now",
        return_value=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
    ):
        result = record_skipped_claim(
            batch_id="batch-1",
            workflow_id="wf-1",
            claim_id="CLM-3",
            excel_payload={"total_billed": 100},
            prior=prior,
            skip_reason=SKIP_REASON_PRIOR_CLEAN,
            run_id="skip-1",
        )
    assert result["status"] == "SKIPPED"
    assert result["skip_reason"] == SKIP_REASON_PRIOR_CLEAN
    assert result["reused_from_run_id"] == "prior-1"
    assert result["total_billed"] == 100
    mock_run_model.objects.create.assert_called_once()
    create_kwargs = mock_run_model.objects.create.call_args.kwargs
    assert create_kwargs["status"] == "SKIPPED"
    assert create_kwargs["claim_payload"]["_skip_reason"] == SKIP_REASON_PRIOR_CLEAN


def test_skip_message_variants():
    assert "duplicate claim in batch" in _skip_message(
        SKIP_REASON_BATCH_DUPLICATE, "run-1",
    )
    assert "prior CLEAN run" in _skip_message(
        SKIP_REASON_PRIOR_CLEAN, "run-2",
    )
