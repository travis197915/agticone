"""Auto-build a builder workflow canvas from an ingested SOP.

Add-on feature: when a workflow is created with ``auto_build_from_sop=true``,
the SOP-ingestion pipeline runs as usual and, on completion, this module
turns the resulting ``AuditSop`` rows into a full builder graph that mirrors
the hand-built **OBH Claims Audit Flow**:

  * one ``WorkArea`` for the workflow,
  * one ``Workbench`` per ingested ``AuditSop`` (a job may ingest several
    linked docs),
  * one ``Shape`` per ``AuditStep`` — ``diamond`` for decision points,
    ``round-rectangle`` for terminal steps, ``rectangle`` otherwise. Steps
    whose decision table spans multiple sections (the pipeline tags each rule
    with ``[{section}]``) are decomposed into one decision node per section,
    mirroring the hand-built OBH Claims Audit Flow,
  * EVERY ``AuditDecision`` row (all nesting depths / row indexes) and every
    precondition ``llm_rule`` bound as a ``NodeRuleBinding`` so no SOP line is
    dropped,
  * linear step-order ``ShapeConnection``s (runtime routing such as
    "skip to step 4" rides on the rules' ``goto_step`` / ``is_out_of_scope``
    and is honored by the execution engine, not the visual edges),
  * ``tool_calls`` left EMPTY — the user is prompted to attach tools.

This module never touches the existing create/ingestion/build code paths; it
is only invoked from the post-ingestion hook when the opt-in flag is set.
"""
from __future__ import annotations

import logging
import re

from django.db import models, transaction
from django.utils.text import slugify

from builder.bindings_sync import extract_bindings_from_properties
from builder.models import (Shape, ShapeConnection, ShapeDefinition, WorkArea,
                            Workbench, Workflow, WorkflowVersion)
from builder.workbench_versioning import content_unchanged, find_matching_workbench
from builder.workflow_versioning import snapshot_workflow_version
from sop_ingestion.models import (AuditDecision, AuditPrecondition, AuditSop,
                                  AuditStep)
from uhc_execution_engine.rule_loader import (_hydrate_decision,
                                              _hydrate_precondition)

log = logging.getLogger(__name__)

# Canvas geometry.
_COL_W = 360.0
_ROW_H = 140.0
_SHAPE_W = 240.0
_SHAPE_H = 90.0
# Zig-zag x offsets within a column (mirrors the OBH reference flowchart).
_ZIG = (0.0, 160.0)

# A step whose decision table has more rules than this — and no section tags
# to group by — is chunked into "part" nodes so no single node hoards the SOP.
_MAX_RULES_PER_NODE = 6

_SECTION_TAG = re.compile(r"^\[([^\]]+)\]")


def _decision_group(d) -> str:
    """Generic grouping key for one AuditDecision row.

    The pipeline prefixes each exception/table rule's summary with its source
    section (``[GE] THEN …``, ``[MPI] THEN …``); nested YAML imports carry
    ``table_name``. Either marker groups the step's rules into separate
    decision nodes. Rows with no marker fall into the anonymous group ''.
    """
    m = _SECTION_TAG.match(d.action_summary or "")
    if m:
        return m.group(1).strip()
    return (d.table_name or "").strip()


def _split_decisions(decisions: list) -> list[tuple[str, list]]:
    """Decompose a step's decision rows into (group_label, rows) buckets.

    * multiple section groups → one bucket per section (order preserved);
    * a NESTED decision table (any row at depth > 0) → a single bucket in
      document order so every parent stays glued to its sub-rules (Step 7's
      "If/And/Then" table is one node with its hierarchy intact, mirroring the
      YAML template where the step is one rule with nested ``sub_rules``);
    * a FLAT list longer than ``_MAX_RULES_PER_NODE`` → numbered parts;
    * otherwise a single bucket.
    """
    groups: dict[str, list] = {}
    order: list[str] = []
    for d in decisions:
        g = _decision_group(d)
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(d)

    if len(order) > 1:
        return [(g, groups[g]) for g in order]

    rows = decisions
    # A nested table must never be sliced: chunking by N tears parents away
    # from their children. Keep the whole tree as one node, ordered by the
    # step-global ``row_index`` (parent immediately followed by its children).
    if any(getattr(d, "depth", 0) for d in rows):
        return [("", sorted(rows, key=lambda d: (d.row_index, d.id)))]

    if len(rows) > _MAX_RULES_PER_NODE:
        chunks = [rows[i:i + _MAX_RULES_PER_NODE]
                  for i in range(0, len(rows), _MAX_RULES_PER_NODE)]
        return [(f"part {i + 1}", c) for i, c in enumerate(chunks)]
    return [("", rows)]


def _shape_defs() -> tuple[ShapeDefinition, ShapeDefinition, ShapeDefinition]:
    rectangle = ShapeDefinition.objects.filter(slug="rectangle").first()
    diamond = ShapeDefinition.objects.filter(slug="diamond").first()
    terminator = ShapeDefinition.objects.filter(slug="round-rectangle").first()
    if rectangle is None:
        raise RuntimeError(
            "ShapeDefinition 'rectangle' not found — the builder catalog is "
            "populated by a data migration; run `python manage.py migrate`.")
    diamond = diamond or rectangle
    terminator = terminator or rectangle
    return rectangle, diamond, terminator


def _dedupe_by_key(rules: list[dict]) -> list[dict]:
    """Keep the first rule per ``key``.

    ``rule_key`` is unique per (shape) by construction (the importer assigns a
    step-global ``row_index``), but a duplicate would raise an IntegrityError
    inside the atomic build and poison the whole transaction. Dedupe defensively
    so one anomalous row can never abort the entire workflow build.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for r in rules:
        k = r.get("key") or ""
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _step_detail(sop: AuditSop, source_ref: str, step, decisions: list) -> str:
    """Full, untrimmed detail for one step, so the inspector shows complete
    context for auto-built nodes."""
    parts = [
        f"SOP: {sop.title}  ·  {source_ref}",
        f"Step {step.step_number}"
        + (f" (yaml_rule_id={step.yaml_rule_id})" if step.yaml_rule_id else ""),
    ]
    if step.question:
        parts.append("")
        parts.append(step.question.strip())
    if step.intro_text:
        parts.append("")
        parts.append(step.intro_text.strip())
    if step.is_out_of_scope:
        parts.append("\n[OUT OF SCOPE — auditing stops on this path when met]")
    if step.is_terminal:
        parts.append(f"\n[TERMINAL step — action {step.terminal_action or ''}]")
    parts.append("")
    parts.append(f"Decision rows ({len(decisions)}):")
    for d in decisions:
        cond = " AND ".join(p for p in [d.condition_if, d.condition_and] if p)
        act = (d.action_text or "").strip()
        rid = d.subrule_id or f"row{d.row_index}"
        line = f"  • [{rid}] {d.decision_type or 'CONDITIONAL'}"
        if cond:
            line += f" — IF: {cond.strip()}"
        if act:
            line += f" — THEN: {act}"
        if d.goto_step is not None:
            line += f" (→ step {d.goto_step})"
        parts.append(line)
        if d.output_text:
            parts.append(f"      Output: {d.output_text.strip()}")
    return "\n".join(parts)


def _workbench_description(sop: AuditSop, source_ref: str, steps: list) -> str:
    """Column-header context mirroring the reference builder."""
    lines = [
        f"Source      : {source_ref}",
        f"SOP         : {sop.title} (AuditSop #{sop.id})",
    ]
    if sop.purpose:
        lines.append(f"Purpose     : {sop.purpose}")
    lines.append(f"What it does: runs {len(steps)} step(s) in this column, "
                 "in order.")
    lines.append("")
    lines.append("Steps:")
    for step in steps:
        q = (step.question or "").strip().replace("\n", " ")
        lines.append(f"  • Step {step.step_number}: {q}")
    return "\n".join(lines)


def _precondition_rules(sop: AuditSop) -> list[dict]:
    """Every precondition llm_rule for the SOP, hydrated (pre: keys)."""
    rules: list[dict] = []
    for pc in AuditPrecondition.objects.filter(sop=sop).order_by("display_order", "id"):
        for idx, _ in enumerate(pc.llm_rules or []):
            rules.append(_hydrate_precondition(sop, pc, idx, "", ""))
    return _dedupe_by_key(rules)


def build_workflow_from_sop(workflow, sop: AuditSop, *, area: WorkArea,
                            col: int, version: int = 1,
                            node_key: str | None = None) -> dict:
    """Build one Workbench (+ shapes + rule bindings) for a single SOP.

    ``version`` is the content version this Workbench represents (see
    ``builder.workbench_versioning``) — callers appending a new version of an
    existing slot pass ``old.version + 1`` and ``node_key=old.node_key`` so the
    two rows are recognisable as the same slot's history.

    Returns a stats dict and the (first_shape, last_shape) endpoints so the
    caller can chain SOP->SOP.
    """
    rectangle, diamond, terminator = _shape_defs()

    steps = list(AuditStep.objects.filter(sop=sop).order_by("step_number", "id"))
    title = sop.title or f"SOP #{sop.id}"
    # Mirror the reference naming: "{n}. {Title}  ·  {source}". The reference
    # cites the YAML path; HTML-ingested SOPs cite their source URL instead.
    source_ref = sop.url or ""
    bench = Workbench.objects.create(
        work_area=area,
        # Cap at the column's varchar(255). Frontend uploads cite a long
        # ``file://…/sop_uploads/<uuid>_<filename>`` source_ref that can push
        # the composed name past 255 and abort the whole canvas build.
        name=(f"{col + 1}. {title}"
              + (f"  ·  {source_ref}" if source_ref else ""))[:255],
        order=col,
        node_key=node_key or slugify(title)[:128],
        version=version,
        kind="SOP",
        description=_workbench_description(sop, source_ref, steps),
        config={
            "sop_id": sop.id,
            "sop_title": sop.title,
            "yaml_ref": source_ref,
            "source_url": sop.url,
            "purpose": sop.purpose or "",
            "step_count": len(steps),
            "does": title,
            # Comparison point for workbench_versioning.content_unchanged() —
            # lets a later re-ingest tell whether this slot's content changed.
            "content_hash": getattr(sop, "content_hash", "") or "",
        },
        position_x=col * _COL_W,
        position_y=0,
        width=_SHAPE_W + 80,
    )

    stats = {"shapes": 0, "rules": 0, "tool_prompt_shapes": []}
    first_shape: Shape | None = None
    prev_shape: Shape | None = None
    row = 0

    # ── Optional gating shape carrying every precondition rule ────────────────
    # The pipeline also materialises preconditions as a real "Step 0" AuditStep
    # (pg_precondition_writer). When that step exists with decision rows it is
    # the canonical pre-step — mirroring the reference workflow, which has
    # "Step 0: ..." nodes instead of a synthetic Preconditions shape. Only fall
    # back to the synthetic shape when no Step 0 was written.
    has_step0 = any(
        s.step_number == 0 and AuditDecision.objects.filter(step=s).exists()
        for s in steps
    )
    pre_rules = [] if has_step0 else _precondition_rules(sop)
    if pre_rules:
        pre_shape = Shape.objects.create(
            workbench=bench,
            definition=diamond if len(pre_rules) >= 2 else rectangle,
            label="Preconditions",
            description="Gating checks evaluated before the decision tree.",
            position_x=col * _COL_W,
            position_y=(row + 1) * _ROW_H,
            width=_SHAPE_W, height=_SHAPE_H,
            order=row,
            properties={
                "sop_rules": pre_rules,
                "tool_calls": [],
                "sop_id": sop.id,
                "sop_title": sop.title,
                "kind": "preconditions",
            },
        )
        extract_bindings_from_properties(pre_shape)
        stats["shapes"] += 1
        stats["rules"] += len(pre_rules)
        stats["tool_prompt_shapes"].append(
            {"shape_id": str(pre_shape.id), "label": "Preconditions",
             "step_number": None})
        first_shape = pre_shape
        prev_shape = pre_shape
        row += 1

    # ── Shapes per step; decision tables decompose into decision nodes ────────
    for step in steps:
        decisions = list(
            AuditDecision.objects.filter(step=step).order_by(
                "depth", "row_index", "id"))
        question = (step.question or "").strip()
        base_label = (f"Step {step.step_number}"
                      + (f": {question}" if question else ""))

        # One node per rule group: a multi-section decision table becomes
        # several decision diamonds (generic — driven by the [section] tags /
        # table_name the pipeline writes for ANY html), never one mega-node.
        buckets = _split_decisions(decisions) if decisions else [("", [])]

        for group_label, rows in buckets:
            sop_rules = _dedupe_by_key(
                [_hydrate_decision(sop, step, d, "", "") for d in rows])

            if step.is_terminal:
                definition = terminator
            elif len(rows) >= 2 or (len(buckets) > 1 and rows):
                definition = diamond     # decision point
            else:
                definition = rectangle

            label = base_label
            if group_label:
                label = f"{base_label} — {group_label}"
            detail = _step_detail(sop, source_ref, step, rows)
            shape = Shape.objects.create(
                workbench=bench,
                definition=definition,
                label=label,
                description=detail,
                position_x=col * _COL_W + _ZIG[row % 2],
                position_y=(row + 1) * _ROW_H,
                width=_SHAPE_W, height=_SHAPE_H,
                order=row,
                properties={
                    "sop_rules": sop_rules,
                    "tool_calls": [],
                    "sop_id": sop.id,
                    "sop_title": sop.title,
                    "yaml_ref": source_ref,
                    "step_number": step.step_number,
                    "step_question": question,
                    "step_detail": detail,
                    **({"rule_group": group_label} if group_label else {}),
                },
            )
            extract_bindings_from_properties(shape)
            stats["shapes"] += 1
            stats["rules"] += len(sop_rules)
            stats["tool_prompt_shapes"].append(
                {"shape_id": str(shape.id), "label": label,
                 "step_number": step.step_number})

            if prev_shape is not None:
                # Out the bottom handle, in the top handle — input and output
                # must never share an edge anchor (matches the hand-built
                # reference: bottom-source → top-target).
                ShapeConnection.objects.create(
                    source_shape=prev_shape, target_shape=shape, label="next",
                    source_port="bottom-source", target_port="top-target")
            if first_shape is None:
                first_shape = shape
            prev_shape = shape
            row += 1

    stats["first_shape"] = first_shape
    stats["last_shape"] = prev_shape
    return stats


def _reuse_existing_sops_for_job(job) -> list:
    """Resolve the existing current AuditSop(s) for a job whose ingestion was
    short-circuited (UNCHANGED re-ingest wrote nothing).

    Matches on the job's seed_url (canonical/url/filename). When the URL has
    several ``is_current`` rows (a known versioning data issue), pick the single
    best one — most AuditSteps, then most recent id — so the canvas is built
    from a fully-populated SOP rather than an empty duplicate.
    """
    seed = (getattr(job, "seed_url", "") or "").strip()
    if not seed:
        return []
    seed_no_slash = seed.rstrip("/")
    tail = seed_no_slash.rsplit("/", 1)[-1]
    try:
        candidates = list(
            AuditSop.objects.filter(is_current=True).filter(
                models.Q(canonical_url__iexact=seed_no_slash)
                | models.Q(url__iexact=seed_no_slash)
                | models.Q(url__icontains=tail)
            )
        )
    except Exception as exc:  # pragma: no cover
        log.warning("auto-build reuse lookup failed for job %s: %s",
                    getattr(job, "job_id", "?"), exc)
        return []
    if not candidates:
        return []

    # One SOP per URL — best row wins (most steps, then newest id).
    def _score(s):
        try:
            n_steps = AuditStep.objects.filter(sop=s).count()
        except Exception:
            n_steps = 0
        return (n_steps, s.id)

    best_by_url: dict[str, "AuditSop"] = {}
    for s in candidates:
        key = (s.canonical_url or s.url or f"sop:{s.id}").rstrip("/")
        cur = best_by_url.get(key)
        if cur is None or _score(s) > _score(cur):
            best_by_url[key] = s
    return list(best_by_url.values())


def _resolve_all_sops(workflow, job) -> list[AuditSop]:
    """Every SOP a from-scratch build for this workflow should include.

    A workflow can accumulate N SOPs over time (the SOPs panel "+" attaches
    more ingestion jobs), so this spans ALL of the workflow's jobs — one SOP
    per source URL (fall back to title), latest ingestion wins — not just the
    job that triggered this call.
    """
    all_sops = list(
        AuditSop.objects.filter(job__workflow=workflow)
        .order_by("job__created_at", "crawl_depth", "id"))
    if not all_sops:
        # CLI / synthetic jobs may not be FK'd to the workflow.
        all_sops = list(
            AuditSop.objects.filter(job=job).order_by("crawl_depth", "id"))

    # Dedupe by source URL (fall back to title) — latest ingestion wins.
    by_key: dict[str, AuditSop] = {}
    for s in all_sops:
        by_key[(s.url or "").strip() or (s.title or f"sop:{s.id}")] = s
    sops = list(by_key.values())

    if not sops:
        # The job wrote NO AuditSop rows — almost always because the
        # revision/version gate marked the doc UNCHANGED (already ingested), so
        # the pipeline skipped synthesis/persist and the real AuditSop is FK'd
        # to an EARLIER job/workflow. Reuse that existing current AuditSop so a
        # freshly-created workflow still gets a canvas instead of hanging on
        # "Building canvas…".
        sops = _reuse_existing_sops_for_job(job)
        if not sops:
            log.warning(
                "auto-build: job %s produced no AuditSop rows for workflow %s "
                "(seed_url=%s) and no existing current AuditSop matched — canvas "
                "will be EMPTY.",
                job.job_id, getattr(workflow, "id", "?"),
                getattr(job, "seed_url", ""),
            )
            return []
        log.warning(
            "auto-build: job %s wrote no AuditSop (UNCHANGED re-ingest); REUSING "
            "existing AuditSop(s) %s for workflow %s.",
            job.job_id, [s.id for s in sops], getattr(workflow, "id", "?"),
        )
    return sops


def _full_build(workflow, sops: list[AuditSop]) -> dict:
    """Build the full canvas from scratch given a resolved SOP list.

    Destructive: clears any existing WorkAreas first. Only safe to call when
    the workflow has no canvas yet to preserve as history — see
    ``sync_workflow_from_job`` for the incremental, history-preserving path
    used once a workflow already has one. Enforced below: once any
    WorkflowVersion snapshot exists, some live Workbench may be referenced by
    a WorkflowVersionWorkbench (PROTECT), and this teardown would raise a raw
    ProtectedError instead of the clear error a caller can act on.
    """
    with transaction.atomic():
        if WorkflowVersion.objects.filter(workflow=workflow).exists():
            raise ValueError(
                f"_full_build refused: workflow {workflow.id} already has "
                "version history — use sync_workflow_from_job's incremental "
                "path instead of a destructive full rebuild."
            )
        workflow.work_areas.all().delete()  # clean rebuild
        area = WorkArea.objects.create(
            workflow=workflow, name="Claim Audit", order=0,
            width=_COL_W * len(sops) + _COL_W, height=_ROW_H * 28,
        )

        total = {"shapes": 0, "rules": 0, "sops": len(sops)}
        tool_prompt_shapes: list[dict] = []
        endpoints: list[tuple] = []
        for col, sop in enumerate(sops):
            st = build_workflow_from_sop(workflow, sop, area=area, col=col)
            total["shapes"] += st["shapes"]
            total["rules"] += st["rules"]
            tool_prompt_shapes.extend(st["tool_prompt_shapes"])
            endpoints.append((st.get("first_shape"), st.get("last_shape")))

        # Chain SOP -> SOP (last shape of bench i -> first shape of bench i+1).
        for i in range(len(endpoints) - 1):
            _, last = endpoints[i]
            nxt_first, _ = endpoints[i + 1]
            if last is not None and nxt_first is not None:
                ShapeConnection.objects.create(
                    source_shape=last, target_shape=nxt_first, label="then",
                    source_port="bottom-source", target_port="top-target")

        meta = dict(workflow.metadata or {})
        meta["needs_tools"] = True
        meta["tool_prompt_shapes"] = tool_prompt_shapes
        meta["auto_build_stats"] = {
            "sops": total["sops"], "shapes": total["shapes"],
            "rules": total["rules"],
        }
        meta["auto_build_complete"] = True
        workflow.metadata = meta
        workflow.save(update_fields=["metadata", "updated_at"])

        # First-ever snapshot for this workflow — lands on "Workflow v1"
        # (Workflow.version is never incremented for the initial build; see
        # builder.workflow_versioning.snapshot_workflow_version).
        snapshot_workflow_version(workflow, reason="initial_build")

    _log_fidelity(workflow, sops, total)
    return total


def _log_fidelity(workflow, sops: list[AuditSop], total: dict) -> None:
    # Full-fidelity check (logged, non-fatal). Precondition llm_rules only
    # count when the SOP has no Step 0 — otherwise Step 0's decision rows ARE
    # the canonical precondition rules (see build_workflow_from_sop).
    expected_rules = 0
    for s in sops:
        expected_rules += AuditDecision.objects.filter(step__sop=s).count()
        has_step0 = AuditDecision.objects.filter(
            step__sop=s, step__step_number=0).exists()
        if not has_step0:
            expected_rules += sum(
                len(pc.llm_rules or [])
                for pc in AuditPrecondition.objects.filter(sop=s))
    if total["rules"] != expected_rules:
        log.warning(
            "auto-build fidelity mismatch wf=%s: bound %d rules, expected %d",
            workflow.id, total["rules"], expected_rules)
    log.info(
        "auto-build done wf=%s sops=%d shapes=%d rules=%d (expected %d)",
        workflow.id, total["sops"], total["shapes"], total["rules"],
        expected_rules)


def build_workflow_for_job(workflow, job) -> dict:
    """Build the full canvas from EVERY SOP ingested for this workflow.

    Always a full, destructive rebuild (clears and recreates every WorkArea).
    Kept for existing direct callers (CLI / tests) that want that original
    semantics. The post-ingestion hook uses ``sync_workflow_from_job`` instead,
    which preserves history and only touches the SOP(s) that actually changed.
    """
    sops = _resolve_all_sops(workflow, job)
    if not sops:
        return {"shapes": 0, "rules": 0, "sops": 0}
    return _full_build(workflow, sops)


def _mark_build_complete(workflow) -> None:
    """Flip ``metadata.auto_build_complete`` back to True after a sync pass.

    ``build_status`` (builder/views.py) gates its ``phase`` entirely on this
    flag, and every re-ingestion dispatch resets it to False before the job
    starts (see ``WorkflowViewSet.attach``). ``_full_build`` sets it back to
    True for a workflow's very first build, but the incremental path here
    used to return without ever setting it back — so any workflow that
    already had a canvas (which is effectively all of them, since the SPA
    seeds an empty WorkArea at creation time) stayed on the "building" phase
    forever even after the sync succeeded. Re-read metadata from the DB
    rather than using the possibly-stale in-memory ``workflow.metadata``.
    """
    workflow.refresh_from_db(fields=["metadata"])
    meta = dict(workflow.metadata or {})
    if not meta.get("auto_build_complete"):
        meta["auto_build_complete"] = True
        workflow.metadata = meta
        workflow.save(update_fields=["metadata", "updated_at"])


def _current_work_area(workflow) -> WorkArea | None:
    return workflow.work_areas.order_by("order").first()


def _next_column(area: WorkArea) -> int:
    max_order = (
        Workbench.objects.filter(work_area=area, is_current=True)
        .aggregate(models.Max("order"))["order__max"])
    return 0 if max_order is None else max_order + 1


def _sync_one_sop(workflow, sop: AuditSop) -> dict:
    """Reconcile one freshly-ingested SOP against the workflow's current
    Workbenches: no-op if unchanged, append a new version if its slot's
    content changed, or add a new column if it matches no existing slot.

    Does NOT snapshot a WorkflowVersion itself, and no longer bumps
    Workflow.version directly — sync_workflow_from_job calls
    snapshot_workflow_version once after every SOP in a job has been synced,
    so one ingestion job produces one coherent WorkflowVersion (and one
    Workflow.version increment) rather than one per SOP. See
    builder.workflow_versioning.
    """
    match = find_matching_workbench(workflow, sop)
    if match is not None and content_unchanged(match, sop):
        return {"shapes": 0, "rules": 0, "sops": 0, "versions_bumped": 0}

    with transaction.atomic():
        if match is not None:
            old = Workbench.objects.select_for_update().get(pk=match.pk)
            st = build_workflow_from_sop(
                workflow, sop, area=old.work_area, col=old.order,
                version=old.version + 1, node_key=old.node_key)
            old.is_current = False
            old.save(update_fields=["is_current", "updated_at"])
        else:
            area = _current_work_area(workflow)
            st = build_workflow_from_sop(
                workflow, sop, area=area, col=_next_column(area))

    return {
        "shapes": st["shapes"], "rules": st["rules"], "sops": 1,
        # A new SOP being added counts as a real composition change too
        # (previously this branch reported 0 and Workflow.version never
        # bumped for a new SOP — fixed here).
        "versions_bumped": 1,
    }


def sync_workflow_from_job(workflow, job) -> dict:
    """Incremental, history-preserving replacement for
    ``build_workflow_for_job``.

    On a workflow's first-ever build there is no history to preserve, so this
    delegates straight to ``_full_build`` (identical to the old behaviour).
    On every later call, only the SOP(s) this particular ``job`` ingested are
    reconciled against the workflow's existing current Workbenches — an
    unrelated Workbench for a different SOP is never touched, deleted, or
    rebuilt. See ``builder.workbench_versioning`` for the slot-matching and
    change-detection rules.
    """
    if workflow.work_areas.count() == 0:
        sops = _resolve_all_sops(workflow, job)
        if not sops:
            return {"shapes": 0, "rules": 0, "sops": 0}
        return _full_build(workflow, sops)

    job_sops = list(AuditSop.objects.filter(job=job).order_by("crawl_depth", "id"))
    if not job_sops:
        # UNCHANGED re-ingest short-circuit (see _resolve_all_sops) — nothing
        # new was written, so there is nothing to reconcile.
        log.info(
            "auto-build sync: job %s wrote no AuditSop rows for workflow %s; "
            "nothing to sync.", job.job_id, workflow.id,
        )
        _mark_build_complete(workflow)
        return {"shapes": 0, "rules": 0, "sops": 0, "versions_bumped": 0}

    total = {"shapes": 0, "rules": 0, "sops": 0, "versions_bumped": 0}
    changed_titles: list[str] = []
    for sop in job_sops:
        st = _sync_one_sop(workflow, sop)
        for key in total:
            total[key] += st[key]
        if st["sops"]:
            changed_titles.append(sop.title or f"sop:{sop.id}")

    # One snapshot for the whole job, not one per SOP — a job that changes
    # both A and B produces a single new WorkflowVersion = A(new)+B(new),
    # matching the product's "one ingestion event = one coherent workflow
    # milestone" model. Each SOP's own Workbench build above still runs in
    # its own transaction (_sync_one_sop, unchanged), so a failure partway
    # through a multi-doc job still durably keeps whichever SOPs already
    # synced — only the final snapshot call is consolidated.
    if total["versions_bumped"]:
        reason = ("sop_sync:" + ",".join(changed_titles))[:64]
        snapshot_workflow_version(workflow, reason=reason)

    _mark_build_complete(workflow)

    log.info(
        "auto-build sync done wf=%s job=%s sops_touched=%d shapes=%d rules=%d "
        "versions_bumped=%d", workflow.id, job.job_id, total["sops"],
        total["shapes"], total["rules"], total["versions_bumped"],
    )
    return total
