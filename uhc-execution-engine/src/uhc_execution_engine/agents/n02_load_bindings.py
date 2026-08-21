"""n02 — load_bindings: hydrate rules + tool bindings for the workflow."""
from __future__ import annotations

import logging
import time

from ..rule_loader import load_workflow_bindings
from ..state import ExecutionState

logger = logging.getLogger(__name__)

_MAX_LOAD_ATTEMPTS = 3


def _current_workflow_version(workflow_id: str) -> int | None:
    from builder.models import Workflow
    return Workflow.objects.filter(id=workflow_id).values_list("version", flat=True).first()


def _load_bindings_snapshot(workflow_id: str) -> dict:
    """One consistent-as-possible read of everything a claim run needs to know
    about the workflow's current configuration: rule bindings, tool bindings,
    execution-mode metadata, and the version label. Wrapped by
    :func:`load_bindings` in a before/after ``Workflow.version`` stability
    check — see that function's retry loop for why a single call here is not
    on its own guaranteed consistent."""
    loaded = load_workflow_bindings(workflow_id)

    # Workflow-level execution mode drives whether SOPs short-circuit on the
    # first defect (linear) or all run and the verdict is fused (parallel).
    # ``supported_lob`` (optional) lets a workflow declare which Lines of
    # Business it audits; a claim whose LOB is not in that set is out of scope.
    execution_mode = "linear"
    supported_lob: list[str] = []
    workflow_version: int | None = None
    try:
        from builder.models import Workflow
        wf = Workflow.objects.filter(id=workflow_id).only("metadata", "version").first()
        if wf:
            meta = wf.metadata or {}
            execution_mode = str(meta.get("execution_mode") or "linear").lower()
            raw_lob = meta.get("supported_lob") or meta.get("supported_lobs") or []
            if isinstance(raw_lob, str):
                raw_lob = [raw_lob]
            supported_lob = [str(x).strip() for x in raw_lob if str(x).strip()]
            workflow_version = wf.version
    except Exception:
        execution_mode = "linear"
    if execution_mode not in {"linear", "parallel"}:
        execution_mode = "linear"

    shapes = loaded["shapes"]

    # Version snapshot — records exactly which Workbench content versions were
    # actually bound for this run, so RuleExecutionRun stays accurate even
    # after a later re-ingest appends a new version and moves the canvas
    # forward (see builder.workbench_versioning).
    workbench_versions: dict[str, dict] = {}
    workbench_ids = {s["workbench_id"] for s in shapes if s.get("workbench_id")}
    workflow_version_id: str | None = None
    if workbench_ids:
        from builder.models import Workbench, WorkflowVersion

        for wb in Workbench.objects.filter(id__in=workbench_ids, is_current=True):
            cfg = wb.config or {}
            workbench_versions[str(wb.id)] = {
                "node_key": wb.node_key,
                "version": wb.version,
                "sop_id": cfg.get("sop_id"),
                "sop_title": cfg.get("sop_title"),
            }

        # Resolve the WorkflowVersion whose slot set EXACTLY equals the
        # Workbench ids actually bound above — deliberately NOT "whichever
        # is latest for this workflow", which could point at a composition
        # different from what was actually loaded if a concurrent ingestion
        # lands mid-request. Because a superseded Workbench row is never
        # mutated (see builder.models.Workbench), a given set of bound
        # Workbench ids can match at most one WorkflowVersion's slot set, so
        # an exact match is unambiguous when one exists, and None (never
        # guessed) when it doesn't — e.g. a pre-migration workflow with no
        # snapshot history yet. Plain Python set-comparison over this
        # workflow's (typically small) version history rather than a
        # filter+annotate(Count()) query, to sidestep the well-known Django
        # gotcha where a preceding filter() on a related field collapses the
        # join a subsequent Count() on that same relation would need.
        for wfv in (
            WorkflowVersion.objects
            .filter(workflow_id=workflow_id)
            .order_by("-version_number")
            .prefetch_related("slots")
        ):
            slots = list(wfv.slots.all())
            if {str(s.workbench_id) for s in slots} == workbench_ids:
                workflow_version_id = str(wfv.id)
                for s in slots:
                    key = str(s.workbench_id)
                    if key in workbench_versions:
                        workbench_versions[key]["sop_version_number"] = s.sop_version_number
                break

    return {
        "loaded": loaded,
        "execution_mode": execution_mode,
        "supported_lob": supported_lob,
        "workflow_version": workflow_version,
        "workbench_versions": workbench_versions,
        "workflow_version_id": workflow_version_id,
    }


def load_bindings(state: ExecutionState) -> dict:
    t0 = time.time()
    stages = list(state.get("stages") or [])
    if state.get("status") == "FAILED":
        return {}

    workflow_id = state["workflow_id"]

    # A claim must execute against ONE consistent configuration: the rule
    # bindings, the tool bindings, and the recorded workflow_version/
    # workflow_version_id must all describe the same moment in time. Under
    # Postgres's default READ COMMITTED isolation, each statement inside
    # _load_bindings_snapshot gets its own fresh snapshot — an approval
    # (SOP rollout, canvas rule-change, or the tool/new-node auto-versioning
    # in WorkflowGraphWriter.save) committing in the middle of that sequence
    # can otherwise produce a run whose bindings are self-consistently from
    # one side of the commit but whose recorded workflow_version is from the
    # other. Bracket the whole read with a before/after Workflow.version
    # check and retry the entire snapshot (bounded) if it moved, rather than
    # locking the Workflow row for every claim start (which would serialize
    # claim throughput behind every approval for no correctness gain).
    try:
        snapshot = None
        for attempt in range(1, _MAX_LOAD_ATTEMPTS + 1):
            v_before = _current_workflow_version(workflow_id)
            snapshot = _load_bindings_snapshot(workflow_id)
            v_after = _current_workflow_version(workflow_id)
            if v_before == v_after:
                break
            logger.warning(
                "load_bindings workflow=%s version changed mid-load (%s -> %s) "
                "on attempt %d/%d — retrying for a consistent read",
                workflow_id, v_before, v_after, attempt, _MAX_LOAD_ATTEMPTS,
            )
        else:
            logger.warning(
                "load_bindings workflow=%s did not stabilize after %d attempts; "
                "proceeding with the last read (best effort)",
                workflow_id, _MAX_LOAD_ATTEMPTS,
            )
        loaded = snapshot["loaded"]
        execution_mode = snapshot["execution_mode"]
        supported_lob = snapshot["supported_lob"]
        workflow_version = snapshot["workflow_version"]
        workbench_versions = snapshot["workbench_versions"]
        workflow_version_id = snapshot["workflow_version_id"]
    except Exception as exc:
        stages.append({"node": "load_bindings", "status": "FAIL",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": str(exc)})
        return {"status": "FAILED", "error_message": f"load_bindings: {exc}",
                "stages": stages}

    # ── Identify the claim's Line of Business (SOW deliverable) ──────────────
    # Derived from the already-fetched claim payload (no extra API call) and
    # surfaced on the claim so every downstream rule-eval prompt sees it.
    from ..lob import determine_claim_lob
    claim = dict(state.get("claim") or {})
    claim_lob = determine_claim_lob(claim, state.get("raw_fetch") or {})
    claim["line_of_business"] = claim_lob["label"]
    # Surface the derived Coverage/Benefit (CBD) path "<Payer> > <LOB>" so the
    # coverage rule-eval prompt reasons over the real plan path (e.g.
    # "Avmed > Commercial") instead of the legacy Medicare stub. Only set when
    # the payer is confidently derivable (never fabricate a path).
    if claim_lob.get("cbd_path"):
        claim["cbd_coverage_path"] = claim_lob["cbd_path"]
    lob_out_of_scope = bool(
        supported_lob
        and claim_lob["product"] not in supported_lob
        and claim_lob["label"] not in supported_lob
    )
    logger.info(
        "load_bindings claim=%s lob=%s out_of_scope=%s (supported=%s)",
        state.get("claim_id") or "-", claim_lob["label"], lob_out_of_scope,
        supported_lob or "all",
    )

    pre = loaded["preconditions"]
    dec = loaded["decisions"]
    shapes = loaded["shapes"]
    shapes_with_rules = sum(1 for s in shapes if s.get("rules"))
    n_tools = len(loaded["all_tool_bindings"])

    # Pre-execution breadcrumb. INFO so it shows for every claim — this is
    # the single most useful line when diagnosing "the engine ran but did
    # nothing": it tells you exactly what the loader produced before any
    # downstream node has a chance to silently skip an empty list.
    logger.info(
        "load_bindings workflow=%s claim=%s shapes=%d with_rules=%d pre=%d dec=%d tools=%d",
        state.get("workflow_id"), state.get("claim_id") or "-",
        len(shapes), shapes_with_rules, len(pre), len(dec), n_tools,
    )

    # The v2 evaluator iterates `shapes`. A workflow is "rule-less" only
    # when every shape grouping is empty and the legacy flat lists are too.
    has_any_rule = any(s.get("rules") for s in shapes) or bool(pre) or bool(dec)
    if not has_any_rule:
        logger.warning(
            "load_bindings workflow=%s has zero rule bindings; aborting run as FAILED",
            state.get("workflow_id"),
        )
        stages.append({"node": "load_bindings", "status": "FAIL",
                       "ms": int((time.time() - t0) * 1000),
                       "msg": "workflow has no attached rules"})
        return {"status": "FAILED",
                "error_message": "workflow has no attached rules",
                "stages": stages}

    # Loader accepted the workflow but the executor will see nothing to do.
    # This is the silent-failure case documented in EXECUTION_ENGINE.md §9.
    if shapes_with_rules == 0 and (pre or dec):
        logger.warning(
            "load_bindings workflow=%s has %d pre + %d dec on flat lists but no "
            "shape-attached rules; execute_shapes will be a no-op and the claim "
            "will fall through to default ALLOW. Likely a migration / rule_loader "
            "issue, not a real adjudication.",
            state.get("workflow_id"), len(pre), len(dec),
        )

    stages.append({"node": "load_bindings", "status": "OK",
                   "ms": int((time.time() - t0) * 1000),
                   "msg": f"{len(shapes)} shapes / {len(pre)} pre / {len(dec)} dec / "
                          f"{n_tools} tools / mode={execution_mode}"})
    return {
        "preconditions": pre,
        "decisions": dec,
        "shapes": shapes,
        "tools_by_rule_key": loaded["tools_by_rule_key"],
        "tools_by_shape": loaded["tools_by_shape"],
        "execution_mode": execution_mode,
        "claim": claim,
        "claim_lob": claim_lob,
        "lob_out_of_scope": lob_out_of_scope,
        "workflow_version": workflow_version,
        "workbench_versions": workbench_versions,
        "workflow_version_id": workflow_version_id,
        "stages": stages,
    }
