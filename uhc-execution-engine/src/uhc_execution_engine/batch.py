"""Outer batch runner: xlsx → per-claim fetch + parse → inner pipeline.

Two entry points:

* :meth:`BatchRunner.run_xlsx` — synchronous; runs the whole batch and
  returns the aggregated dict.  Used by the existing
  ``POST /api/execute/workflows/<id>/run-batch/`` view.
* :meth:`BatchRunner.iter_xlsx` — generator; yields one event dict per
  claim (plus ``batch_start`` and ``summary`` envelopes).  Used by the
  Celery task that backs the streaming endpoint, which republishes each
  event to Redis pub/sub.  ``run_xlsx`` is now implemented as a thin
  collector around ``iter_xlsx`` so both paths exercise the same code.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Iterator

from django.utils import timezone

from .claim_fetcher import (fetch_claim, fetch_tool_error, parse_claim,
                            resolve_fetch_tool, workflow_uses_parser)
from .duplicate_claim import (SKIP_REASON_BATCH_DUPLICATE,
                              SKIP_REASON_PRIOR_CLEAN, find_prior_clean_run,
                              record_skipped_claim)
from .mcp_client import (check_mcp_health, format_mcp_health_error,
                         should_run_mcp_health_check)
from .pipeline import RuleEnginePipeline
from .rule_loader import load_workflow_bindings
from .xlsx_parser import XlsxParseError, extract_claim_rows, EXCEL_BILLING_FIELDS

logger = logging.getLogger(__name__)

_EXCEL_RESPONSE_KEYS = EXCEL_BILLING_FIELDS


def _excel_fields(excel_row: dict[str, Any] | None) -> dict[str, Any]:
    if not excel_row:
        return {}
    return {
        k: excel_row[k]
        for k in _EXCEL_RESPONSE_KEYS
        if excel_row.get(k) not in (None, "")
    }


def _apply_excel_metadata(claim: dict[str, Any],
                          excel_row: dict[str, Any] | None) -> dict[str, Any]:
    fields = _excel_fields(excel_row)
    if not fields:
        return claim
    merged = dict(claim)
    merged.update(fields)
    return merged


def _claim_from_fetch(claim_id: str, raw_fetch_result: dict[str, Any],
                      parsed: dict[str, Any] | None,
                      excel_row: dict[str, Any] | None = None) -> dict[str, Any]:
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
    return _apply_excel_metadata(base, excel_row)


class BatchRunner:
    """End-to-end runner for an uploaded Excel of claim ids."""

    def __init__(self):
        self._pipeline = RuleEnginePipeline()

    # ── Streaming entrypoint ────────────────────────────────────────────────

    def iter_xlsx(
        self, *, workflow_id: str, xlsx_bytes: bytes,
        filename: str = "claims.xlsx",
        claim_id_column: str | None = None,
        sheet_name: str | None = None,
        batch_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield batch_start → claim* → summary events.

        ``batch_id`` may be supplied when the caller (e.g. the kickoff view)
        has already created the ``BatchExecutionRun`` row — typical for the
        async path where the SPA needs the id in the 202 response.  When
        omitted, this method creates the row itself (sync path).

        Yielded shapes::

            {"kind": "batch_start", "batch_id": "...", "total_claims": N,
             "workflow_id": "...", "claim_id_column": "...",
             "source_filename": "..."}
            {"kind": "claim", "result": <per-claim response dict>}
            ...
            {"kind": "summary", "batch": {
                "id": "...", "status": "COMPLETED|PARTIAL|FAILED",
                "total_claims": N, "completed": X, "failed": Y,
                "duration_ms": ...}}

        On a fatal pre-run error (xlsx parse failure, no rule bindings)
        yields a single ``summary`` event with ``status="FAILED"`` and
        ``error_message`` populated, and returns.
        """
        from execution_app.models import BatchExecutionRun

        batch_id = batch_id or str(uuid.uuid4())
        t0 = time.time()

        # ── 1. Parse the workbook ──────────────────────────────────────────
        try:
            claim_rows, resolved_col = extract_claim_rows(
                xlsx_bytes, claim_id_column=claim_id_column,
                sheet_name=sheet_name)
        except XlsxParseError as exc:
            yield {
                "kind": "summary",
                "batch": {
                    "id": batch_id, "status": "FAILED",
                    "total_claims": 0, "completed": 0, "failed": 0,
                    "duration_ms": int((time.time() - t0) * 1000),
                    "error_message": str(exc),
                },
            }
            return

        # ── 2. Load workflow bindings (lets us know if the parser tool
        #      is bound, so we can run it once per claim in the outer layer).
        try:
            loaded = load_workflow_bindings(workflow_id)
        except Exception as exc:
            yield {
                "kind": "summary",
                "batch": {
                    "id": batch_id, "status": "FAILED",
                    "total_claims": len(claim_rows), "completed": 0, "failed": 0,
                    "duration_ms": int((time.time() - t0) * 1000),
                    "error_message": f"load_workflow_bindings: {exc}",
                },
            }
            return
        use_parser = workflow_uses_parser(loaded["all_tool_bindings"])
        fetch_tool = resolve_fetch_tool(workflow_id, loaded["all_tool_bindings"])

        # ── 3. Reserve / fetch the BatchExecutionRun row ──────────────────
        # If the caller pre-created it (async path), update it with the
        # parsed metadata; otherwise create it now.
        batch, created = BatchExecutionRun.objects.get_or_create(
            id=batch_id,
            defaults=dict(
                workflow_id=str(workflow_id),
                source_filename=filename,
                claim_id_column=resolved_col,
                total_claims=len(claim_rows),
                status="RUNNING",
            ),
        )
        if not created:
            # Pre-created: patch in the values the kickoff view couldn't
            # know until we actually parsed the workbook.
            BatchExecutionRun.objects.filter(id=batch_id).update(
                source_filename=filename,
                claim_id_column=resolved_col,
                total_claims=len(claim_rows),
                status="RUNNING",
            )

        if not fetch_tool:
            err = fetch_tool_error(loaded["all_tool_bindings"])
            BatchExecutionRun.objects.filter(id=batch_id).update(
                status="FAILED",
                error_message=err,
                finished_at=timezone.now(),
            )
            yield {
                "kind": "summary",
                "batch": {
                    "id": batch_id, "status": "FAILED",
                    "total_claims": len(claim_rows), "completed": 0, "failed": 0,
                    "duration_ms": int((time.time() - t0) * 1000),
                    "error_message": err,
                },
            }
            return

        # ── 4. batch_start envelope ────────────────────────────────────────
        yield {
            "kind": "batch_start",
            "batch_id": batch_id,
            "workflow_id": str(workflow_id),
            "total_claims": len(claim_rows),
            "claim_id_column": resolved_col,
            "source_filename": filename,
        }

        # ── 5. Per-claim loop ──────────────────────────────────────────────
        completed = 0
        failed = 0
        seen_in_batch: dict[str, dict[str, Any]] = {}
        logger.info("batch=%s starting per-claim loop over %d claim(s)",
                    batch_id, len(claim_rows))
        for row in claim_rows:
            cid = str(row["claim_id"])
            excel_payload = _excel_fields(row)
            original_auditor = str(row.get("original_auditor") or "")
            auditor_status = str(row.get("auditor_status") or "")
            if cid in seen_in_batch:
                res = record_skipped_claim(
                    batch_id=batch_id,
                    workflow_id=str(workflow_id),
                    claim_id=cid,
                    excel_payload=excel_payload,
                    prior=seen_in_batch[cid],
                    skip_reason=SKIP_REASON_BATCH_DUPLICATE,
                    original_auditor=original_auditor,
                    auditor_status=auditor_status,
                )
            else:
                prior_clean = find_prior_clean_run(
                    claim_id=cid,
                    workflow_id=str(workflow_id),
                    exclude_batch_id=batch_id,
                )
                if prior_clean is not None:
                    res = record_skipped_claim(
                        batch_id=batch_id,
                        workflow_id=str(workflow_id),
                        claim_id=cid,
                        excel_payload=excel_payload,
                        prior=prior_clean,
                        skip_reason=SKIP_REASON_PRIOR_CLEAN,
                        original_auditor=original_auditor,
                        auditor_status=auditor_status,
                    )
                else:
                    res = self._run_one(
                        workflow_id=str(workflow_id),
                        claim_id=cid,
                        batch_id=batch_id,
                        use_parser=use_parser,
                        fetch_tool=fetch_tool,
                        excel_row=row,
                    )
                seen_in_batch[cid] = res
            counted_as = ("completed" if res["status"]
                          in {"COMPLETED", "TERMINATED_EARLY", "SKIPPED"}
                          else "failed")
            if counted_as == "completed":
                completed += 1
            else:
                failed += 1
            logger.info(
                "batch=%s claim=%s run=%s status=%s decision=%s counted_as=%s",
                batch_id, cid, res.get("run_id") or "-",
                res.get("status") or "-",
                res.get("final_decision_type") or "-",
                counted_as,
            )
            yield {"kind": "claim", "result": res}

        # ── 6. Finalize the BatchExecutionRun row + emit summary ──────────
        if failed == 0:
            batch_status = "COMPLETED"
        elif completed == 0:
            batch_status = "FAILED"
        else:
            batch_status = "PARTIAL"

        BatchExecutionRun.objects.filter(id=batch_id).update(
            completed=completed,
            failed=failed,
            status=batch_status,
            finished_at=timezone.now(),
        )

        yield {
            "kind": "summary",
            "batch": {
                "id": batch_id,
                "status": batch_status,
                "total_claims": len(claim_rows),
                "completed": completed,
                "failed": failed,
                "duration_ms": int((time.time() - t0) * 1000),
            },
        }

    # ── Synchronous entrypoint ──────────────────────────────────────────────

    def run_xlsx(self, *, workflow_id: str, xlsx_bytes: bytes,
                 filename: str = "claims.xlsx",
                 claim_id_column: str | None = None,
                 sheet_name: str | None = None,
                 batch_id: str | None = None) -> dict[str, Any]:
        """Collect the generator into the legacy aggregate-dict response.

        Same return shape as before. Used by the synchronous
        ``RunBatchView``.
        """
        results: list[dict[str, Any]] = []
        summary: dict[str, Any] | None = None
        bid = batch_id  # captured for the fallback summary below
        for event in self.iter_xlsx(
            workflow_id=workflow_id, xlsx_bytes=xlsx_bytes,
            filename=filename, claim_id_column=claim_id_column,
            sheet_name=sheet_name, batch_id=batch_id,
        ):
            kind = event.get("kind")
            if kind == "batch_start":
                bid = event["batch_id"]
            elif kind == "claim":
                results.append(event["result"])
            elif kind == "summary":
                summary = event["batch"]

        if summary is None:
            # iter_xlsx always yields a summary, even on fatal error,
            # but guard defensively.
            return {
                "batch_id": bid or "",
                "status": "FAILED",
                "total_claims": 0, "completed": 0, "failed": 0,
                "duration_ms": 0,
                "error_message": "no summary event from iter_xlsx",
                "results": results,
            }

        return {
            "batch_id": summary["id"],
            "status": summary["status"],
            "total_claims": summary["total_claims"],
            "completed": summary["completed"],
            "failed": summary["failed"],
            "duration_ms": summary["duration_ms"],
            "error_message": summary.get("error_message", ""),
            "results": results,
        }

    # ── Per-claim worker (unchanged) ────────────────────────────────────────

    def _run_one(self, *, workflow_id: str, claim_id: str,
                 batch_id: str, use_parser: bool,
                 fetch_tool: str | None = None,
                 excel_row: dict[str, Any] | None = None) -> dict[str, Any]:
        from execution_app.models import RuleExecutionRun, ToolInvocationRecord

        excel_payload = _excel_fields(excel_row)
        original_auditor = str((excel_row or {}).get("original_auditor") or "")
        auditor_status = str((excel_row or {}).get("auditor_status") or "")

        if should_run_mcp_health_check(fetch_tool=fetch_tool):
            logger.info(
                "batch: MCP health check starting claim=%s fetch_tool=%s",
                claim_id,
                fetch_tool or "-",
            )
            health = check_mcp_health(tool_name=fetch_tool, claim_id=claim_id)
            if not health.get("ok"):
                run_id = str(uuid.uuid4())
                error_message = format_mcp_health_error(health)
                logger.warning(
                    "batch: MCP health check failed claim=%s tool=%s url=%s error=%s",
                    claim_id,
                    health.get("probed_tool") or fetch_tool or "-",
                    health.get("url") or "-",
                    health.get("error") or "-",
                )
                RuleExecutionRun.objects.create(
                    id=run_id,
                    batch_id=batch_id,
                    workflow_id=workflow_id,
                    claim_id=claim_id,
                    claim_payload=excel_payload,
                    raw_fetch={},
                    finished_at=timezone.now(),
                    status="FAILED",
                    error_message=error_message,
                    original_auditor=original_auditor,
                    auditor_status=auditor_status,
                )
                return {
                    "run_id": run_id,
                    "claim_id": claim_id,
                    "status": "FAILED",
                    "error_message": error_message,
                    "tool_invocations": [],
                    **excel_payload,
                }

        # 1. Fetch the claim via the workflow's configured fetch tool (required;
        #    resolved from metadata or canvas bindings — no silent default).
        fetch_out = fetch_claim(claim_id, tool_name=fetch_tool)
        if not fetch_out["ok"]:
            run_id = str(uuid.uuid4())
            run = RuleExecutionRun.objects.create(
                id=run_id, batch_id=batch_id, workflow_id=workflow_id,
                claim_id=claim_id, claim_payload=excel_payload,
                raw_fetch=fetch_out.get("result") if isinstance(fetch_out.get("result"), dict) else {},
                finished_at=timezone.now(),
                status="FETCH_FAILED",
                error_message=f"{fetch_out['tool']}: {fetch_out['error']}",
                original_auditor=original_auditor,
                auditor_status=auditor_status,
            )
            ToolInvocationRecord.objects.create(
                run=run, tool_name=fetch_out["tool"], phase="FETCH",
                args=fetch_out["args"], ok=False, result={},
                error=fetch_out["error"], duration_ms=fetch_out["duration_ms"],
            )
            return {
                "run_id": run_id, "claim_id": claim_id,
                "status": "FETCH_FAILED",
                "error_message": f"{fetch_out['tool']}: {fetch_out['error']}",
                "tool_invocations": [{
                    "tool": fetch_out["tool"], "phase": "FETCH",
                    "ok": False, "ms": fetch_out["duration_ms"],
                    "error": fetch_out["error"],
                }],
                **excel_payload,
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
        claim = _claim_from_fetch(
            claim_id, fetch_out["result"] or {}, parsed_payload, excel_row,
        )
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
            skip_mcp_health_check=True,
            original_auditor=original_auditor,
            auditor_status=auditor_status,
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
        response.update(excel_payload)
        return response
