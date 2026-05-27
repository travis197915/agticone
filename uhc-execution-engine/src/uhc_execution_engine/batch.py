"""Outer batch runner: xlsx → per-claim fetch + parse → inner pipeline."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from django.utils import timezone

from .claim_fetcher import fetch_claim, parse_claim, workflow_uses_parser
from .pipeline import RuleEnginePipeline
from .rule_loader import load_workflow_bindings
from .xlsx_parser import XlsxParseError, extract_claim_ids

logger = logging.getLogger(__name__)


def _claim_from_fetch(claim_id: str, raw_fetch_result: dict[str, Any],
                      parsed: dict[str, Any] | None) -> dict[str, Any]:
    """Build the claim dict handed to the inner pipeline.

    If `llm_parse_claim_with_ontology` ran successfully, prefer its
    normalized output; otherwise fall back to the raw linx payload with the
    subscriber_id surfaced at the top.
    """
    base: dict[str, Any] = {"subscriber_id": claim_id, "claim_id": claim_id}
    if parsed and isinstance(parsed, dict):
        base.update(parsed)
    elif isinstance(raw_fetch_result, dict):
        # Try common containers from linx_claim_search
        data = raw_fetch_result.get("data") or raw_fetch_result
        if isinstance(data, dict):
            base.update({k: v for k, v in data.items()
                         if k not in base or base[k] in ("", None)})
    return base


class BatchRunner:
    """End-to-end runner for an uploaded Excel of claim ids."""

    def __init__(self):
        self._pipeline = RuleEnginePipeline()

    def run_xlsx(self, *, workflow_id: str, xlsx_bytes: bytes,
                 filename: str = "claims.xlsx",
                 claim_id_column: str | None = None,
                 sheet_name: str | None = None) -> dict[str, Any]:
        from execution_app.models import BatchExecutionRun

        batch_id = str(uuid.uuid4())
        t0 = time.time()
        try:
            claim_ids, resolved_col = extract_claim_ids(
                xlsx_bytes, claim_id_column=claim_id_column, sheet_name=sheet_name)
        except XlsxParseError as exc:
            return {
                "batch_id": batch_id, "status": "FAILED",
                "error_message": str(exc),
                "total_claims": 0, "completed": 0, "failed": 0, "results": [],
            }

        # Pre-load bindings once so we can decide whether to run the parser
        # tool for every claim. (The inner pipeline re-loads in its own node;
        # that's intentional — we keep the outer-layer concern small.)
        try:
            loaded = load_workflow_bindings(workflow_id)
        except Exception as exc:
            return {
                "batch_id": batch_id, "status": "FAILED",
                "error_message": f"load_workflow_bindings: {exc}",
                "total_claims": len(claim_ids), "completed": 0, "failed": 0,
                "results": [],
            }
        use_parser = workflow_uses_parser(loaded["all_tool_bindings"])

        batch = BatchExecutionRun.objects.create(
            id=batch_id,
            workflow_id=str(workflow_id),
            source_filename=filename,
            claim_id_column=resolved_col,
            total_claims=len(claim_ids),
            status="RUNNING",
        )

        results: list[dict[str, Any]] = []
        completed = 0
        failed = 0
        for cid in claim_ids:
            res = self._run_one(workflow_id=str(workflow_id), claim_id=cid,
                                batch_id=batch_id, use_parser=use_parser)
            results.append(res)
            # A claim counts as "completed" if the engine reached a verdict
            # for it — including the early-halt path. FETCH_FAILED, FAILED,
            # and RUNNING (shouldn't happen post-pipeline) count as failures.
            if res["status"] in {"COMPLETED", "TERMINATED_EARLY"}:
                completed += 1
            else:
                failed += 1

        if failed == 0:
            batch_status = "COMPLETED"
        elif completed == 0:
            batch_status = "FAILED"
        else:
            batch_status = "PARTIAL"

        batch.completed = completed
        batch.failed = failed
        batch.status = batch_status
        batch.finished_at = timezone.now()
        batch.save(update_fields=["completed", "failed", "status", "finished_at"])

        return {
            "batch_id": batch_id,
            "status": batch_status,
            "total_claims": len(claim_ids),
            "completed": completed,
            "failed": failed,
            "duration_ms": int((time.time() - t0) * 1000),
            "results": results,
        }

    def _run_one(self, *, workflow_id: str, claim_id: str,
                 batch_id: str, use_parser: bool) -> dict[str, Any]:
        from execution_app.models import (RuleExecutionRun,
                                             ToolInvocationRecord)

        # 1. Fetch the claim via linx_claim_search
        fetch_out = fetch_claim(claim_id)
        if not fetch_out["ok"]:
            run_id = str(uuid.uuid4())
            run = RuleExecutionRun.objects.create(
                id=run_id, batch_id=batch_id, workflow_id=workflow_id,
                claim_id=claim_id, claim_payload={},
                raw_fetch=fetch_out.get("result") if isinstance(fetch_out.get("result"), dict) else {},
                finished_at=timezone.now(),
                status="FETCH_FAILED",
                error_message=f"linx_claim_search: {fetch_out['error']}",
            )
            ToolInvocationRecord.objects.create(
                run=run, tool_name=fetch_out["tool"], phase="FETCH",
                args=fetch_out["args"], ok=False, result={},
                error=fetch_out["error"], duration_ms=fetch_out["duration_ms"],
            )
            return {
                "run_id": run_id, "claim_id": claim_id,
                "status": "FETCH_FAILED",
                "error_message": f"linx_claim_search: {fetch_out['error']}",
                "tool_invocations": [{
                    "tool": fetch_out["tool"], "phase": "FETCH",
                    "ok": False, "ms": fetch_out["duration_ms"],
                    "error": fetch_out["error"],
                }],
            }

        # 2. Optional parse step
        parsed_payload: dict[str, Any] | None = None
        outer_invocations: list[dict[str, Any]] = [{
            "tool_name": fetch_out["tool"], "phase": "FETCH",
            "args": fetch_out["args"], "ok": True,
            "result": fetch_out["result"], "error": "",
            "duration_ms": fetch_out["duration_ms"],
            "binding_id": "",
        }]
        if use_parser:
            parse_out = parse_claim(fetch_out["result"] or {})
            outer_invocations.append({
                "tool_name": parse_out["tool"], "phase": "PARSE",
                "args": parse_out["args"], "ok": parse_out["ok"],
                "result": parse_out["result"] if parse_out["ok"] else {},
                "error": parse_out["error"],
                "duration_ms": parse_out["duration_ms"],
                "binding_id": "",
            })
            if parse_out["ok"] and isinstance(parse_out["result"], dict):
                parsed_payload = parse_out["result"]

        # 3. Build the claim dict + invoke the inner pipeline
        claim = _claim_from_fetch(claim_id, fetch_out["result"] or {}, parsed_payload)
        run_id = str(uuid.uuid4())
        # Seed the inner pipeline's tool_invocations with the outer-layer calls
        # so they show up in the response + persistence in one place.
        response = self._pipeline.run(
            workflow_id=workflow_id,
            claim=claim,
            raw_fetch=fetch_out["result"] if isinstance(fetch_out["result"], dict) else {},
            claim_id=claim_id,
            batch_id=batch_id,
            run_id=run_id,
        )

        # Splice outer invocations onto the response + persist them too.
        for inv in outer_invocations:
            try:
                from execution_app.models import (RuleExecutionRun as RR,
                                                     ToolInvocationRecord as TR)
                run = RR.objects.filter(id=run_id).first()
                if run is not None:
                    TR.objects.create(
                        run=run, tool_name=inv["tool_name"], phase=inv["phase"],
                        args=inv["args"], ok=inv["ok"],
                        result=inv["result"] if isinstance(inv["result"], (dict, list))
                                              else {"value": inv["result"]},
                        error=inv["error"], duration_ms=inv["duration_ms"],
                    )
            except Exception as exc:  # pragma: no cover
                logger.warning("batch: outer invocation persist failed (%s)", exc)
        response.setdefault("tool_invocations", [])
        response["tool_invocations"] = [
            {"tool": inv["tool_name"], "phase": inv["phase"],
             "ok": inv["ok"], "ms": inv["duration_ms"],
             "error": inv["error"]} for inv in outer_invocations
        ] + response["tool_invocations"]
        return response
