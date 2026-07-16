"""Resolve reviewer/auditor userIDs (JWT ``sub``) to display names.

``RuleExecutionRun.htl_reviewer`` / ``original_auditor`` store the corebackend
userID, not a name. Callers batch-resolve IDs through here in ONE query per
call — one page/batch of runs never triggers more than a single extra query
against ``claims_corebackend.app_user``, regardless of how many rows are on
the page. That table may live on the same Postgres instance (different
schema, the default) or on a genuinely separate instance (COREBACKEND_DB_HOST
— see sop_backend.db_routers.CorebackendRouter, which this module defers to
transparently); either way this stays a single round trip.
"""
from __future__ import annotations

import logging
from typing import Iterable

from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, router, transaction

from .models import CorebackendUser

logger = logging.getLogger(__name__)

# ImproperlyConfigured (e.g. COREBACKEND_DB_HOST set without
# COREBACKEND_DB_NAME) is raised at connection time, before any query runs —
# it is NOT a DatabaseError subclass, so it needs its own catch below. Either
# way this must degrade, never 500 the whole claims API over a reviewer-name
# side lookup.
_LOOKUP_ERRORS = (DatabaseError, ImproperlyConfigured)


def resolve_reviewer_names(user_ids: Iterable[str]) -> dict[str, str]:
    ids = {uid for uid in user_ids if uid}
    if not ids:
        return {}
    # The router may send this to a wholly separate DB connection
    # (COREBACKEND_DB_HOST) — resolve which alias so the savepoint below
    # actually wraps the connection being queried, not always 'default'.
    alias = router.db_for_read(CorebackendUser) or "default"
    try:
        # Wrapped in its own savepoint so a failure here (e.g. the
        # claims_corebackend schema missing, as in a fresh test DB, or the
        # corebackend instance being unreachable) can't poison an outer
        # transaction/atomic block the caller is in.
        with transaction.atomic(using=alias):
            return dict(
                CorebackendUser.objects.using(alias)
                .filter(id__in=ids).values_list("id", "name")
            )
    except _LOOKUP_ERRORS:
        # claims_corebackend.app_user is owned by another service —
        # degrade to showing the raw userID rather than failing the whole
        # response.
        logger.warning("reviewer name lookup failed for ids=%s", ids, exc_info=True)
        return {}
