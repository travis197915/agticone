"""Decide what a finished ingestion means for the workflow that triggered it.

Uploading a SOP into a workflow is two different events wearing the same
button. The first upload of a document is a *build*: there is no canvas yet,
nothing to compare against, and the existing auto-build path is right. Every
upload after that is an *update*: the workflow is already running rules from
an earlier version of that same document, and silently rebuilding the canvas
would discard the auditor's work and change live behaviour with no review.

This module tells those two apart, using the only evidence that actually
matters — whether the workflow has bindings on an earlier version of the
document that was just ingested. If it does, the ingestion raises a change set
for review instead of touching the canvas, and the canvas keeps running the
old version until a reviewer approves.

Scope is per workflow by construction: the question asked is "what is *this*
workflow bound to", so two workflows on the same document are unaffected by
each other's uploads.
"""
from __future__ import annotations

import logging
from typing import Any

from ..models import AuditSop
from .rule_changes import build_change_set_from_ingestion

logger = logging.getLogger(__name__)

__all__ = ["handle_finished_ingestion"]


def _bound_sops(workflow, new_sop: AuditSop) -> list[AuditSop]:
    """Every SOP version this workflow currently has rule bindings on."""
    from agent_tools.models import NodeRuleBinding

    bound_ids = (
        NodeRuleBinding.objects
        .filter(shape__workbench__work_area__workflow=workflow)
        .exclude(sop_id=new_sop.id)
        .values_list("sop_id", flat=True)
        .distinct()
    )
    return list(
        AuditSop.objects.filter(id__in=list(bound_ids))
        .order_by("-version_number", "-id")
    )


def _previous_sop_for(workflow, new_sop: AuditSop) -> AuditSop | None:
    """The version of ``new_sop``'s document this workflow is bound to, if any.

    Two ways to establish "same document", because the platform has two upload
    routes and only one of them keeps a stable identity:

    1. **Same ``SopDocument``.** The exact answer. Holds when the SOP arrived
       by the same URL, since ``register_sop_version`` keys documents on
       ``canonical_url``.
    2. **Same title, among the SOPs this workflow is bound to.** Needed because
       ``WorkflowViewSet._store_sop_upload`` stores every upload as
       ``{uuid4}_{name}``, so re-uploading a revised file through the modal
       produces a brand-new URL, hence a new ``SopDocument`` and a version
       chain restarting at 1. Without this fallback the update would look like
       a first upload and silently rebuild a live canvas.

    The fallback is a heuristic, but a tightly-scoped one: the candidates are
    only the handful of SOPs this workflow already runs, not the whole estate.

    Returns None when there is no previous version — the safe answer, since the
    caller then builds rather than reviews.
    """
    candidates = _bound_sops(workflow, new_sop)
    if not candidates:
        return None

    matched = [s for s in candidates if s.document_id
               and s.document_id == new_sop.document_id]
    matched_by = "document"

    if not matched:
        title = _norm_title(new_sop.title)
        matched = [s for s in candidates if title and _norm_title(s.title) == title]
        matched_by = "title"
        if matched:
            logger.info(
                "ingestion review: sop=%s matched previous sop=%s by title, not "
                "document — a fresh upload URL restarted the version chain",
                new_sop.id, matched[0].id,
            )

    if not matched:
        return None
    if len(matched) > 1:
        # A canvas straddling two versions of one document should not happen —
        # the rollout moves every binding together. Take the newest and say so
        # rather than failing the ingestion.
        logger.warning(
            "workflow=%s has bindings on %d versions of the same document "
            "(matched by %s); diffing against the newest (sop=%s)",
            getattr(workflow, "id", None), len(matched), matched_by, matched[0].id,
        )
    return matched[0]


def _norm_title(title: str) -> str:
    return " ".join((title or "").split()).casefold()


def _chain_version(previous: AuditSop, new_sop: AuditSop) -> bool:
    """Attach a freshly-uploaded SOP onto the version chain it belongs to.

    ``register_sop_version`` establishes a chain by looking the prior version up
    on ``canonical_url``. That works for a re-crawl of the same URL, but never
    for the modal: ``WorkflowViewSet._store_sop_upload`` writes every upload as
    ``{uuid4}_{name}``, so each one has a URL nothing has seen before. The
    registry finds no prior, mints a new ``SopDocument``, and starts again at
    ``version_number = 1``.

    The visible result is two SOPs both badged **v1 / Current / Active**, a list
    that grows by one entry per upload, and a review header reading "v1 → v1".
    Naming uploads by filename would not help — a revision routinely arrives as
    a differently-named file.

    By the time this runs we have already decided these are two versions of one
    document (see :func:`_previous_sop_for`), which is precisely the fact the
    registry lacked. So finish the job it could not: adopt the previous
    version's document, take the next version number, and record ``supersedes``.

    **Identity only — this does not activate anything.** Chaining says "these
    are the same document"; it does not say "the new one is live". Activation
    is the reviewer's call and is applied by :func:`hold_pending_review` /
    ``activate_sop_version`` further down. An earlier version of this function
    also set ``is_current``/``ACTIVE`` here, copying ``register_sop_version``'s
    auto-activate branch, which left the workflow *executing* v1 while the UI
    badged v1 "Superseded / Not approved" and v2 "Active / Current / Approved"
    — precisely backwards.

    Returns True when a repair was made. The ``SopDocument`` the upload minted
    is left in place, not deleted — other rows may reference it, and an orphan
    row is harmless next to the risk of cascading a delete through real data.
    """
    if new_sop.document_id and new_sop.document_id == previous.document_id:
        return False  # same-URL re-crawl: the registry already chained it
    if previous.document_id is None:
        return False  # nothing to adopt

    document = previous.document
    new_sop.document = document
    new_sop.version_number = previous.version_number + 1
    new_sop.supersedes = previous
    new_sop.save(update_fields=["document", "version_number", "supersedes"])

    logger.info(
        "ingestion review: chained sop=%s onto document=%s as v%s (supersedes %s)",
        new_sop.id, document.id, new_sop.version_number, previous.id,
    )
    return True


def hold_pending_review(new_sop: AuditSop, previous: AuditSop) -> None:
    """Park a newly-ingested version until a reviewer accepts it.

    The workflow keeps executing ``previous`` until its change set is approved,
    so ``previous`` is what "current" has to mean. Ingestion alone is not
    adoption.

    ``PENDING_REVIEW`` is the platform's existing word for this — it is what
    ``register_sop_version(auto_activate=False)`` sets, what
    ``activate_sop_version`` demands as its precondition, and what the SOP badge
    strip already renders in amber. Using it keeps this flow and the pre-existing
    activate/reject flow speaking the same language.
    """
    from django.db import transaction

    from ..models import ActivationStatus, SopDocument

    with transaction.atomic():
        if new_sop.is_current or new_sop.activation_status != ActivationStatus.PENDING_REVIEW:
            new_sop.is_current = False
            new_sop.activation_status = ActivationStatus.PENDING_REVIEW
            new_sop.save(update_fields=[
                "is_current", "activation_status", "updated_at",
            ])

        # The registry may already have activated the new version (a same-URL
        # re-crawl auto-activates) and superseded the one still in force. Put
        # the running version back, or the canvas executes a SOP the UI calls
        # superseded.
        if previous.activation_status != ActivationStatus.ACTIVE or not previous.is_current:
            previous.is_current = True
            previous.activation_status = ActivationStatus.ACTIVE
            previous.save(update_fields=[
                "is_current", "activation_status", "updated_at",
            ])

        if previous.document_id:
            SopDocument.objects.filter(pk=previous.document_id).update(
                current_version=previous,
            )

    logger.info(
        "ingestion review: sop=%s held at pending_review; sop=%s stays current",
        new_sop.id, previous.id,
    )


def _activate_quietly(new_sop: AuditSop, previous: AuditSop) -> None:
    """Adopt a re-ingest that turned out to change nothing.

    There is no review to wait for, so holding it at ``pending_review`` would
    leave a version parked forever with no one able to clear it. Routed through
    ``activate_sop_version`` so supersession and ``document.current_version``
    follow the same rules as the manual activate flow.
    """
    from ..models import ActivationStatus
    from .versioning import activate_sop_version

    if new_sop.activation_status != ActivationStatus.PENDING_REVIEW:
        new_sop.is_current = False
        new_sop.activation_status = ActivationStatus.PENDING_REVIEW
        new_sop.save(update_fields=["is_current", "activation_status", "updated_at"])
    try:
        activate_sop_version(new_sop.id, reviewed_by="ingestion")
    except Exception as exc:  # pragma: no cover - never fail an ingest for this
        logger.warning("could not activate identical re-ingest sop=%s: %s",
                       new_sop.id, exc)


def _mark_build_terminal(workflow, change_set_ids: list[int]) -> None:
    """Tell the SPA the build screen is over, because there is no build.

    ``WorkflowViewSet.attach`` clears ``auto_build_complete`` on every upload so
    the progress screen replays, and ``build_status`` reports ``building`` until
    something sets it back. Normally ``build_workflow_for_job`` does. On the
    review path nothing builds — the canvas deliberately stays on the old
    version — so without this the SPA sits on "Building canvas…" forever.

    ``done`` is the honest phase here: the canvas is complete and usable, it is
    simply the *previous* version until a reviewer approves. ``pending_sop_review``
    carries the batch ids so the workflow view can surface the review instead.
    """
    from django.db import transaction

    from builder.models import Workflow

    with transaction.atomic():
        locked = Workflow.objects.select_for_update().get(pk=workflow.pk)
        meta = dict(locked.metadata or {})
        meta["auto_build_complete"] = True
        meta["pending_sop_review"] = change_set_ids
        locked.metadata = meta
        locked.save(update_fields=["metadata", "updated_at"])
    workflow.metadata = meta


def handle_finished_ingestion(job) -> dict[str, Any]:
    """Raise reviews for a finished job, or report that it is a first build.

    Returns ``{"is_update": bool, "change_sets": [...], "silent": [...],
    "chained": [...]}``.

    ``is_update`` is the signal the caller needs: when true the canvas must be
    left alone. It is true even when no change set was raised, because an
    unchanged re-upload is still an update — there is simply nothing to review,
    and rebuilding the canvas would be just as wrong.
    """
    workflow = getattr(job, "workflow", None)
    result: dict[str, Any] = {
        "is_update": False, "change_sets": [], "silent": [], "chained": [],
    }
    if workflow is None:
        return result

    author = (getattr(workflow, "owner_email", "") or "").strip() or "ingestion"

    for new_sop in AuditSop.objects.filter(job=job).select_related("document"):
        previous = _previous_sop_for(workflow, new_sop)
        if previous is None:
            continue  # first upload of this document into this workflow

        result["is_update"] = True
        # Do this before building the batch: the review header renders
        # "v{from} → v{to}" off ``version_number``, which is still 1 on both
        # sides until the chain is repaired.
        if _chain_version(previous, new_sop):
            result["chained"].append(new_sop.id)

        change_set = build_change_set_from_ingestion(
            workflow=workflow,
            from_sop=previous,
            to_sop=new_sop,
            job=job,
            author=author,
        )
        if change_set is None:
            # Decision 12: an ingest with nothing to say stays silent. The
            # workflow keeps running `previous`, which is now provably
            # equivalent, so there is nothing to roll forward either — and
            # nothing to review, so the new version can take over directly.
            result["silent"].append(new_sop.id)
            _activate_quietly(new_sop, previous)
            logger.info(
                "ingestion review: sop %s -> %s is identical for workflow=%s",
                previous.id, new_sop.id, workflow.id,
            )
            continue

        # A reviewer now owns this version. Until they accept it, the workflow
        # still executes `previous`, so `previous` stays the current version.
        hold_pending_review(new_sop, previous)

        result["change_sets"].append(change_set.id)
        logger.info(
            "ingestion review: change set %s raised for workflow=%s (sop %s -> %s)",
            change_set.id, workflow.id, previous.id, new_sop.id,
        )

    if result["is_update"]:
        # Includes the silent case: an identical re-upload still suppressed the
        # build, so the progress screen still has to be released.
        _mark_build_terminal(workflow, result["change_sets"])

    return result
