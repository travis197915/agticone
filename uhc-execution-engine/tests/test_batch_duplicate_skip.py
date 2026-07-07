"""Integration-style tests for batch duplicate skipping (mocked ORM)."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone as dt_timezone
from unittest import mock

import pytest

from uhc_execution_engine.batch import BatchRunner
from uhc_execution_engine.duplicate_claim import (
    SKIP_REASON_BATCH_DUPLICATE,
    SKIP_REASON_PRIOR_CLEAN,
)


@pytest.fixture
def runner() -> BatchRunner:
    return BatchRunner()


def _batch_models_mock():
    batch = mock.Mock()
    batch_model = mock.Mock()
    batch_model.objects.get_or_create.return_value = (batch, True)
    batch_model.objects.filter.return_value.update.return_value = 1
    return mock.Mock(BatchExecutionRun=batch_model), batch_model


@mock.patch("uhc_execution_engine.batch.load_workflow_bindings")
@mock.patch("uhc_execution_engine.batch.resolve_fetch_tool")
@mock.patch("uhc_execution_engine.batch.workflow_uses_parser")
@mock.patch("uhc_execution_engine.batch.extract_claim_rows")
def test_iter_xlsx_skips_duplicate_rows_in_same_batch(
    mock_extract,
    mock_use_parser,
    mock_fetch_tool,
    mock_bindings,
    runner: BatchRunner,
):
    mock_extract.return_value = (
        [{"claim_id": "CLM-A"}, {"claim_id": "CLM-A"}],
        "claim_id",
    )
    mock_bindings.return_value = {"all_tool_bindings": []}
    mock_use_parser.return_value = False
    mock_fetch_tool.return_value = "facets_get_claim_summary"
    models_mock, _ = _batch_models_mock()
    now = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)

    with mock.patch(
        "uhc_execution_engine.batch.timezone.now",
        return_value=now,
    ), mock.patch.dict(sys.modules, {"execution_app.models": models_mock}), mock.patch(
        "uhc_execution_engine.batch.find_prior_clean_run",
        return_value=None,
    ), mock.patch.object(
        runner,
        "_run_one",
        return_value={
            "run_id": "run-1",
            "claim_id": "CLM-A",
            "status": "COMPLETED",
            "final_decision_type": "APPROVE",
            "applied_codes": [],
            "narrative": "done",
            "tool_invocations": [],
        },
    ) as run_one, mock.patch(
        "uhc_execution_engine.batch.record_skipped_claim",
        side_effect=lambda **kwargs: {
            "run_id": "skip-1",
            "claim_id": kwargs["claim_id"],
            "status": "SKIPPED",
            "skip_reason": kwargs["skip_reason"],
            "reused_from_run_id": "run-1",
            "tool_invocations": [],
        },
    ) as skip_claim:
        events = list(
            runner.iter_xlsx(
                workflow_id=str(uuid.uuid4()),
                xlsx_bytes=b"fake",
                batch_id=str(uuid.uuid4()),
            )
        )

    run_one.assert_called_once()
    skip_claim.assert_called_once()
    assert skip_claim.call_args.kwargs["skip_reason"] == SKIP_REASON_BATCH_DUPLICATE
    claim_events = [e for e in events if e.get("kind") == "claim"]
    assert len(claim_events) == 2
    assert claim_events[1]["result"]["status"] == "SKIPPED"


@mock.patch("uhc_execution_engine.batch.load_workflow_bindings")
@mock.patch("uhc_execution_engine.batch.resolve_fetch_tool")
@mock.patch("uhc_execution_engine.batch.workflow_uses_parser")
@mock.patch("uhc_execution_engine.batch.extract_claim_rows")
def test_iter_xlsx_skips_when_prior_clean_exists(
    mock_extract,
    mock_use_parser,
    mock_fetch_tool,
    mock_bindings,
    runner: BatchRunner,
):
    mock_extract.return_value = ([{"claim_id": "CLM-B"}], "claim_id")
    mock_bindings.return_value = {"all_tool_bindings": []}
    mock_use_parser.return_value = False
    mock_fetch_tool.return_value = "facets_get_claim_summary"
    models_mock, _ = _batch_models_mock()
    prior = mock.Mock(id="prior-run")
    now = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)

    with mock.patch(
        "uhc_execution_engine.batch.timezone.now",
        return_value=now,
    ), mock.patch.dict(sys.modules, {"execution_app.models": models_mock}), mock.patch.object(
        runner, "_run_one",
    ) as run_one, mock.patch(
        "uhc_execution_engine.batch.find_prior_clean_run",
        return_value=prior,
    ), mock.patch(
        "uhc_execution_engine.batch.record_skipped_claim",
        return_value={
            "run_id": "skip-2",
            "claim_id": "CLM-B",
            "status": "SKIPPED",
            "skip_reason": SKIP_REASON_PRIOR_CLEAN,
            "reused_from_run_id": "prior-run",
            "tool_invocations": [],
        },
    ):
        events = list(
            runner.iter_xlsx(
                workflow_id=str(uuid.uuid4()),
                xlsx_bytes=b"fake",
                batch_id=str(uuid.uuid4()),
            )
        )

    run_one.assert_not_called()
    claim_events = [e for e in events if e.get("kind") == "claim"]
    assert claim_events[0]["result"]["skip_reason"] == SKIP_REASON_PRIOR_CLEAN
