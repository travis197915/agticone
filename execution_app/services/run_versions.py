"""Which version of a workflow's rules a run executed against.

A run is a frozen record of the rules that produced it. Once a workflow is
updated — an ingestion change set approved, its bindings repointed at a new
``AuditSop`` — every later run uses different rules, and the two are no longer
comparable. The listing has to say which is which, or an auditor looking at two
rows for the same claim cannot tell them apart.

Nothing stores this today, and nothing needs to: ``RuleEvaluation.rule_key`` is
``step:{sop_id}:{step_number}:{row_index}`` (and ``pre:{sop_id}:…`` for
preconditions), so the set of SOP ids a run touched is already recorded on every
evaluation it wrote. This module reads it back.

Two questions get answered, and they are not the same one:

``version_label``
    What to show beside the claim id — the highest ``version_number`` among the
    SOPs the run used. A workflow spans several SOPs (nine, on the live
    workflow), so there is no single version; the max is what moves when any of
    them is revised, which is exactly when two runs need telling apart.

``is_outdated``
    Whether a re-run would use different rules. Compares the run's SOP ids
    against the workflow's *current* bindings — ground truth, not a version
    number, because a repointed binding names a different ``AuditSop`` row
    entirely.

Everything is batched per page: three queries regardless of how many runs.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Iterable

from django.db.models import Func, Value
from django.db.models.functions import Cast
from django.db.models import IntegerField

logger = logging.getLogger(__name__)

__all__ = ["run_version_info", "RunVersion"]

# A run in one of these states never produced a verdict, so "reprocess" means
# retry rather than "the rules moved on" — but it is the same button.
_FAILED_STATUSES = frozenset({"FETCH_FAILED", "FAILED", "ERROR"})


class _SplitPart(Func):
    """Postgres ``split_part`` — pulls the sop id out of a rule_key in SQL.

    Doing this in the database keeps the whole page to one query instead of
    dragging ~238 rule_keys per run into Python just to read one field.
    """

    function = "split_part"
    output_field = IntegerField()


RunVersion = dict[str, Any]


def _sop_ids_by_run(run_ids: list[str]) -> dict[str, set[int]]:
    """``{run_id: {sop_id, …}}`` from the rule_keys each run wrote.

    Only recognizes ``step:``/``pre:`` (SOP-derived) rule_keys — a run whose
    matched rules are entirely ``custom:{uuid}`` (no SOP at all) legitimately
    has no entry here. That's fine for this function's own purpose (the
    SOP-id-set fallback comparison), but callers must NOT use "absent here"
    as a proxy for "never executed" — see :func:`_evaluated_run_ids`.
    """
    from execution_app.models import RuleEvaluation

    if not run_ids:
        return {}
    rows = (
        RuleEvaluation.objects
        .filter(run_id__in=run_ids)
        # A malformed key would make split_part return '' and the cast fail, so
        # keep to the two shapes the builder emits.
        .filter(rule_key__regex=r"^(step|pre):[0-9]+:")
        .annotate(sop_id=Cast(_SplitPart("rule_key", Value(":"), Value(2)),
                              IntegerField()))
        .values_list("run_id", "sop_id")
        .distinct()
    )
    out: dict[str, set[int]] = defaultdict(set)
    for run_id, sop_id in rows:
        if sop_id:
            out[str(run_id)].add(sop_id)
    return dict(out)


def _evaluated_run_ids(run_ids: list[str]) -> set[str]:
    """Run ids that wrote at least one ``RuleEvaluation`` row, of ANY
    rule_key shape — SOP-derived (``step:``/``pre:``) or custom
    (``custom:{uuid}``). This is the correct "did it ever run" check.

    Deliberately separate from :func:`_sop_ids_by_run`, which only recognizes
    SOP-derived keys: a claim run entirely on custom rules would otherwise be
    mislabeled ``never_ran`` (no SOP-shaped rule_key to find) even though it
    executed and produced a verdict — exactly the kind of custom-rule blind
    spot this module exists to close.
    """
    from execution_app.models import RuleEvaluation

    if not run_ids:
        return set()
    return set(
        str(rid) for rid in
        RuleEvaluation.objects.filter(run_id__in=run_ids)
        .values_list("run_id", flat=True).distinct()
    )


def _current_sop_ids_by_workflow(workflow_ids: Iterable[Any]) -> dict[str, set[int]]:
    """``{workflow_id: {sop_id, …}}`` the canvas is bound to right now."""
    from agent_tools.models import NodeRuleBinding

    workflow_ids = [w for w in workflow_ids if w]
    if not workflow_ids:
        return {}
    rows = (
        NodeRuleBinding.objects
        .filter(shape__workbench__work_area__workflow_id__in=workflow_ids)
        .values_list("shape__workbench__work_area__workflow_id", "sop_id")
        .distinct()
    )
    out: dict[str, set[int]] = defaultdict(set)
    for workflow_id, sop_id in rows:
        if sop_id:
            out[str(workflow_id)].add(sop_id)
    return dict(out)


def _current_workflow_versions(workflow_ids: Iterable[Any]) -> dict[str, int]:
    """``{workflow_id: Workflow.version}`` for every workflow_id given."""
    from builder.models import Workflow

    workflow_ids = [w for w in workflow_ids if w]
    if not workflow_ids:
        return {}
    return {
        str(wid): version
        for wid, version in Workflow.objects.filter(id__in=workflow_ids)
        .values_list("id", "version")
    }


def run_version_info(runs: list) -> dict[str, RunVersion]:
    """``{run_id: {...}}`` describing the rule version each run executed.

    Returns, per run:

    ``version_label``    ``"v2"`` — for the chip beside the claim id
    ``sop_versions``     ``[{sop_id, title, version_number}]`` — the detail
    ``is_outdated``      the workflow has moved on; a re-run would differ
    ``current_label``    what a re-run would produce, when outdated

    ``never_ran``        the run wrote no evaluations; the label is the version
                         it was *dispatched against*, not one it executed

    A run that never reached the rules — a failed fetch, say — has no executed
    version. Reporting nothing left the row blank and unreadable beside the run
    it displaced, so it is labelled with the dispatched version and flagged, and
    a *failed* one still offers reprocess because there "reprocess" means retry.

    **Primary mechanism — ``RuleExecutionRun.workflow_version`` vs. the
    workflow's current ``Workflow.version``.** Both counters are bumped by
    every execution-affecting change (SOP rollout, canvas rule-content
    approval, a brand-new node's rules, or a tool add/edit/delete — see
    ``builder.workflow_versioning.snapshot_workflow_version`` and
    ``builder.services.WorkflowGraphWriter.save``), so a plain integer
    compare uniformly covers all of them — not just SOP changes.

    This only fires when the run has a **reliably captured**
    ``workflow_version`` (``is not None``). A run predates the field, or
    failed before ``n02_load_bindings`` ran long enough to capture it, has no
    trustworthy value to compare — it is **never** marked outdated by this
    comparison, no matter how far ``Workflow.version`` has moved since, and
    instead falls back to the SOP-id-set comparison below (kept alive
    unchanged, exactly as it worked before this mechanism existed) so it
    still gets a best-effort label instead of a false "stale" or a silent
    "up to date".
    """
    from sop_ingestion.models import AuditSop

    if not runs:
        return {}

    run_ids = [str(r.id) for r in runs]
    by_run = _sop_ids_by_run(run_ids)
    evaluated_run_ids = _evaluated_run_ids(run_ids)
    by_workflow = _current_sop_ids_by_workflow({r.workflow_id for r in runs})
    current_workflow_versions = _current_workflow_versions({r.workflow_id for r in runs})

    every_sop_id = {s for ids in by_run.values() for s in ids}
    every_sop_id |= {s for ids in by_workflow.values() for s in ids}
    sops = {
        s.id: s for s in
        AuditSop.objects.filter(id__in=every_sop_id).only(
            "id", "title", "version_number",
        )
    }

    def _label(sop_ids: set[int]) -> str:
        versions = [sops[i].version_number for i in sop_ids if i in sops]
        return f"v{max(versions)}" if versions else ""

    info: dict[str, RunVersion] = {}
    for run in runs:
        run_id = str(run.id)
        ran_on = by_run.get(run_id, set())
        current = by_workflow.get(str(run.workflow_id), set())
        status = str(getattr(run, "status", "") or "").upper()

        # A run that wrote no evaluations never reached the rules — a failed
        # fetch, say. It has no executed version, but it was dispatched against
        # whatever the workflow is bound to, and leaving the row blank makes it
        # unreadable next to the run it was meant to supersede. Label it with
        # the dispatched version and flag that it never ran, so the UI can say
        # "attempted" rather than implying those rules produced a verdict.
        #
        # Checked against ALL evaluations the run wrote (any rule_key shape),
        # not just the SOP-recognized ``ran_on`` set above — a run whose
        # matched rules are entirely custom (``custom:{uuid}``, no SOP at
        # all) legitimately has an empty ``ran_on`` but DID execute, and must
        # not be reported as never having run.
        never_ran = run_id not in evaluated_run_ids

        run_workflow_version = getattr(run, "workflow_version", None)
        current_workflow_version = current_workflow_versions.get(str(run.workflow_id))
        has_reliable_version = (
            run_workflow_version is not None and current_workflow_version is not None
        )

        if has_reliable_version:
            if never_ran:
                # Reprocess here means retry — same semantics as the fallback
                # path below, just no longer gated on the SOP-id detail set.
                outdated = status in _FAILED_STATUSES
            else:
                outdated = run_workflow_version < current_workflow_version
            version_label = f"v{run_workflow_version}"
            current_label = f"v{current_workflow_version}" if outdated else ""
            described = current if never_ran else ran_on
        else:
            described = current if never_ran else ran_on
            if never_ran:
                outdated = status in _FAILED_STATUSES and bool(current)
            else:
                outdated = bool(current) and ran_on != current
            version_label = _label(described)
            current_label = _label(current) if outdated else ""

        info[run_id] = {
            "version_label": version_label,
            "sop_versions": sorted(
                (
                    {
                        "sop_id": i,
                        "title": sops[i].title or f"SOP #{i}",
                        "version_number": sops[i].version_number,
                    }
                    for i in described if i in sops
                ),
                key=lambda d: d["sop_id"],
            ),
            "is_outdated": outdated,
            "never_ran": never_ran,
            "current_label": current_label,
        }
    return info
