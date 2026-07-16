"""Resolve reviewer/auditor userIDs (JWT ``sub``) to display names.

``RuleExecutionRun.htl_reviewer`` / ``original_auditor`` store the corebackend
userID, not a name. Callers batch-resolve IDs through here to avoid N+1
queries against ``claims_corebackend.app_user`` when serializing lists.
"""
from __future__ import annotations

import logging
from typing import Iterable

from django.db import DatabaseError, transaction

from .models import CorebackendUser

logger = logging.getLogger(__name__)


def resolve_reviewer_names(user_ids: Iterable[str]) -> dict[str, str]:
    ids = {uid for uid in user_ids if uid}
    if not ids:
        return {}
    try:
        # Wrapped in its own savepoint so a failure here (e.g. the
        # claims_corebackend schema missing, as in a fresh test DB) can't
        # poison an outer transaction/atomic block the caller is in.
        with transaction.atomic():
            return dict(
                CorebackendUser.objects.filter(id__in=ids).values_list("id", "name")
            )
    except DatabaseError:
        # claims_corebackend.app_user lives in a schema owned by another
        # service — degrade to showing the raw userID rather than failing
        # the whole response.
        logger.warning("reviewer name lookup failed for ids=%s", ids, exc_info=True)
        return {}
