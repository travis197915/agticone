"""Diff the rules of two ingested versions of the same SOP.

When a SOP document is re-uploaded into a workflow, ingestion writes a fresh
``AuditSop`` with brand-new ``AuditDecision`` rows. Nothing links a rule in the
new version to "the same" rule in the old one — primary keys are new, and step
numbers and row indexes shift the moment a step is renumbered or a row inserted.
Pairing them is the whole problem, and it is what makes a diff possible at all.

``rule_reconcile._match_rules`` already solves it for the YAML-compare flow, so
this module reuses it rather than inventing a second matcher. Its cascade is:

1. exact ``subrule_id``
2. a shared unique identifier in the text (TIN / NPI)
3. semantic similarity (OpenAI embeddings)
4. lexical similarity (``difflib``)

with a small bonus for landing on the same step number to break ties. It
degrades safely: no OpenAI key means step 3 is skipped and step 4 carries it.
That matters here because ``subrule_id`` coverage ranges 26%-100% across SOPs,
and re-ingestion measurably *degrades* it — Physician Checklist v2 has 33%
coverage where v1 had 78% — so the fallbacks are the common path, not the
exception.

Assignment is one-to-one, so every rule ends up in exactly one of three
buckets: modified, added, or removed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models import AuditDecision, AuditSop
from ..rule_reconcile import (
    RECONCILABLE_FIELDS,
    _decision_fields,
    _match_rules,
    _rule_blob,
    _sanitize_proposed,
)

__all__ = ["RuleDelta", "diff_sop_versions", "summarise"]


@dataclass
class RuleDelta:
    """One rule's difference between two SOP versions.

    ``kind`` is decided by which side is present:
    both → ``modified``, only new → ``added``, only old → ``removed``.
    """

    kind: str
    from_decision: AuditDecision | None
    to_decision: AuditDecision | None
    previous_fields: dict[str, Any] = field(default_factory=dict)
    proposed_fields: dict[str, Any] = field(default_factory=dict)
    changed_fields: list[str] = field(default_factory=list)

    MODIFIED = "modified"
    ADDED = "added"
    REMOVED = "removed"
    # Matched with nothing to report. Never surfaced for review — it exists so
    # the rollout can repoint a workflow's *whole* set of bindings, not just
    # the ones that changed. See ``include_unchanged``.
    UNCHANGED = "unchanged"

    @property
    def anchor(self) -> AuditDecision:
        """The decision to derive identity and ordering from.

        Prefers the old side so a modified rule keeps the identity the reviewer
        already recognises; falls back to the new side for an addition.
        """
        anchor = self.from_decision or self.to_decision
        assert anchor is not None  # a delta always has at least one side
        return anchor


# Ingestion is not deterministic. Re-running the enrichment pass over an
# unchanged document rewrites rule text cosmetically — it prefixes conditions
# with "If", drops em-dashes, moves punctuation. Measured on a real re-ingest
# of an unedited section: 22 of 57 reported modifications were nothing but
# this, e.g. "Code is to Allow" → "If code is to Allow". Left unfiltered they
# bury the handful of real changes a reviewer is there to judge.
#
# So the diff compares *meaning*, not bytes: a leading connective and all
# punctuation are ignored when deciding whether a field moved. The stored and
# displayed text stays verbatim — this only decides what counts as a change.
#
# It cannot fix a genuine rewording ("No" → "IF status is No" is a real text
# difference and stays flagged); only stabler ingestion can.
_LEADING_CONNECTIVE = re.compile(r"^(?:if|then|and|when|else|or)\b\s*", re.I)
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def _semantic_norm(value: Any) -> str:
    """Casefold, drop leading connectives and all punctuation, collapse space.

    Aggressive on purpose. The cost is that a change consisting *only* of
    punctuation (a comma splitting one code into two) reads as no change; the
    benefit is that the reviewer sees three real edits instead of sixty-eight.
    """
    text = str(value if value is not None else "").casefold()
    previous = None
    while previous != text:  # "if then ..." needs more than one pass
        previous = text
        text = _LEADING_CONNECTIVE.sub("", text.strip(), count=1)
    return _NON_ALNUM.sub(" ", text).strip()


def _material_changes(incoming: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Fields that genuinely moved, keeping the incoming text verbatim.

    Runs ``_sanitize_proposed`` first so field whitelisting, boolean coercion
    and ``decision_type`` validation stay identical to the apply path, then
    drops anything whose difference survives only as punctuation or a leading
    "If"/"THEN".
    """
    changed = _sanitize_proposed(incoming, previous)
    return {
        field_name: value
        for field_name, value in changed.items()
        if not (
            isinstance(value, str)
            and _semantic_norm(value) == _semantic_norm(previous.get(field_name))
        )
    }


def _incoming_payload(decision: AuditDecision) -> dict[str, Any]:
    """Shape a decision into the dict ``_match_rules`` expects for ``incoming``.

    It reads the ``RECONCILABLE_FIELDS`` (via ``_rule_blob``) plus
    ``subrule_id`` and ``step_number``. The decision itself rides along under a
    private key so the object can be recovered after matching — ``_match_rules``
    returns the dicts it was given, not indexes.
    """
    payload = dict(_decision_fields(decision))
    payload["subrule_id"] = (decision.subrule_id or "").strip()
    payload["step_number"] = decision.step.step_number
    payload["_decision"] = decision
    return payload


def _pair_identical_within_step(
    old_decisions: list[AuditDecision],
    new_decisions: list[AuditDecision],
) -> tuple[list[tuple[AuditDecision, AuditDecision]],
           list[AuditDecision], list[AuditDecision]]:
    """Pre-pair rules whose text is identical, before ``_match_rules`` runs.

    ``_match_rules`` treats an exact ``subrule_id`` as its strongest signal —
    correct for the YAML path it was written for, where ids are authored. Here
    they are not: ingestion assigns ``RULE-006-002`` style ids **by row
    position**, so inserting one row renumbers every rule below it. The
    strongest key then points at the wrong rule precisely when a rule was added
    or removed, which is the case the reviewer most needs to be right.

    Seen for real. Step 6 went from::

        v1  RULE-006-001 'Yes'   RULE-006-002 'No'
        v2  RULE-006-001 'Yes'   RULE-006-002 'Medicare Advantage'  RULE-006-003 'No'

    and the id match paired v1's ``No`` with v2's ``Medicare Advantage``,
    reporting v2's ``No`` as a new rule. Similarity was never consulted, which
    is why the lexical and embedding paths returned the same wrong answer even
    though ``No``↔``No`` scores 1.000.

    Identical text is stronger evidence than a positional id, so it goes first.
    Scoped to the same step number: blobs like ``"yes"`` repeat across a
    document, and pairing those across steps would trade one bug for a worse
    one. A step *renumbering* therefore falls through to ``_match_rules``,
    which is what its same-step bonus and similarity scoring are for.

    Matching runs in two tiers, most specific first:

    ``_blob_key``
        Every field ``_rule_blob`` reads. Strongest evidence.
    ``_core_key``
        Condition and action only — what actually defines a rule.

    The second tier exists because requiring the *whole* blob is too brittle to
    survive re-ingestion. Seen for real: one run populated ``applicable_when``
    by echoing the condition and the next run left it empty, so the same
    untouched rule keyed as ``'no next step no'`` against ``'no next step'``,
    missed the exact pass, and was then mispaired by ``subrule_id`` anyway. The
    unstable fields (``applicable_when``, ``output_text``, ``condition_and``)
    are enrichment-derived; condition and action carry the rule's identity.
    """
    def _blob_key(decision: AuditDecision) -> str:
        return _semantic_norm(_rule_blob(_decision_fields(decision)))

    def _core_key(decision: AuditDecision) -> str:
        return _semantic_norm(
            f"{decision.condition_if or ''} {decision.action_text or ''}"
        )

    pairs: list[tuple[AuditDecision, AuditDecision]] = []
    old_rest, new_rest = list(old_decisions), list(new_decisions)
    for key_of in (_blob_key, _core_key):
        tier, old_rest, new_rest = _pair_by_key(old_rest, new_rest, key_of)
        pairs.extend(tier)
    return pairs, old_rest, new_rest


def _pair_by_key(
    old_decisions: list[AuditDecision],
    new_decisions: list[AuditDecision],
    key_of,
) -> tuple[list[tuple[AuditDecision, AuditDecision]],
           list[AuditDecision], list[AuditDecision]]:
    """One exact-match pass over ``(step_number, key_of(decision))``."""
    buckets: dict[tuple[int, str], list[AuditDecision]] = {}
    for decision in new_decisions:
        key = key_of(decision)
        if key:
            buckets.setdefault((decision.step.step_number, key), []).append(decision)

    pairs: list[tuple[AuditDecision, AuditDecision]] = []
    taken: set[int] = set()
    leftover_old: list[AuditDecision] = []
    for old in old_decisions:
        key = key_of(old)
        # Duplicated text within one step is real (several rows saying "Next
        # step."); consume in document order so the pairing stays one-to-one
        # and deterministic.
        candidates = buckets.get((old.step.step_number, key)) if key else None
        match = next((c for c in candidates if c.id not in taken), None) if candidates else None
        if match is None:
            leftover_old.append(old)
            continue
        pairs.append((old, match))
        taken.add(match.id)

    return pairs, leftover_old, [d for d in new_decisions if d.id not in taken]


def _load_decisions(sop: AuditSop) -> list[AuditDecision]:
    return list(
        AuditDecision.objects
        .filter(step__sop=sop)
        .select_related("step")
        .order_by("step__step_number", "row_index")
    )


def _config():
    """EngineConfig for embeddings, or None to force the lexical fallback."""
    try:
        from uhc_execution_engine.config import get_config
        return get_config()
    except Exception:  # pragma: no cover - engine optional at import time
        return None


def diff_sop_versions(
    from_sop: AuditSop,
    to_sop: AuditSop,
    *,
    use_embeddings: bool = True,
    include_unchanged: bool = False,
) -> list[RuleDelta]:
    """Pair the rules of two SOP versions and report what differs.

    By default only genuinely-changed rules are returned — a matched pair whose
    fields are identical is dropped, so an unchanged re-ingest yields an empty
    list and no review is raised.

    Pass ``include_unchanged=True`` to also get ``UNCHANGED`` deltas for those
    identical pairs. The rollout needs them: a workflow's bindings all have to
    move to the new SOP together, and a rule that did not change still has a
    new primary key on the other side. Reviewers must never see these.

    Pass ``use_embeddings=False`` to force the deterministic lexical path (used
    by tests, which should not depend on a provider being reachable).
    """
    old_decisions = _load_decisions(from_sop)
    new_decisions = _load_decisions(to_sop)
    if not old_decisions and not new_decisions:
        return []

    # Settle exact-text pairs first, so a positionally-assigned subrule_id
    # cannot claim a rule that plainly belongs to another.
    exact, old_rest, new_rest = _pair_identical_within_step(
        old_decisions, new_decisions,
    )
    cfg = _config() if use_embeddings else None
    incoming = [_incoming_payload(d) for d in new_rest]
    matched: list[tuple[AuditDecision | None, AuditDecision | None]] = list(exact)
    matched += [
        (old, new["_decision"] if new else None)
        for old, new in _match_rules(incoming, old_rest, cfg)
    ]

    deltas: list[RuleDelta] = []
    for old, new_decision in matched:
        if old is not None and new_decision is not None:
            previous = _decision_fields(old)
            incoming_fields = _decision_fields(new_decision)
            # Wraps the sanitiser the apply path uses — same whitelist, same
            # coercion — then discards differences that are only cosmetic.
            # Safe to filter here because approval no longer replays these
            # fields through ``rule_reconcile.apply()``: the rollout repoints
            # the binding at the new decision, which already holds this text.
            changed = _material_changes(incoming_fields, previous)
            if not changed:
                # Matched and identical — nothing to review, but the rollout
                # still has to carry the binding across to the new rule.
                if include_unchanged:
                    deltas.append(RuleDelta(
                        kind=RuleDelta.UNCHANGED,
                        from_decision=old,
                        to_decision=new_decision,
                        previous_fields=_jsonable(previous),
                    ))
                continue
            deltas.append(RuleDelta(
                kind=RuleDelta.MODIFIED,
                from_decision=old,
                to_decision=new_decision,
                previous_fields=_jsonable(previous),
                proposed_fields=_jsonable(changed),
                changed_fields=sorted(changed),
            ))

        elif new_decision is not None:
            fields = _decision_fields(new_decision)
            deltas.append(RuleDelta(
                kind=RuleDelta.ADDED,
                from_decision=None,
                to_decision=new_decision,
                previous_fields={},
                proposed_fields=_jsonable(fields),
                changed_fields=sorted(f for f in RECONCILABLE_FIELDS if fields.get(f)),
            ))

        elif old is not None:
            fields = _decision_fields(old)
            deltas.append(RuleDelta(
                kind=RuleDelta.REMOVED,
                from_decision=old,
                to_decision=None,
                previous_fields=_jsonable(fields),
                proposed_fields={},
                changed_fields=sorted(f for f in RECONCILABLE_FIELDS if fields.get(f)),
            ))

    deltas.sort(key=lambda d: (d.anchor.step.step_number, d.anchor.row_index))
    return deltas


def summarise(deltas: list[RuleDelta]) -> dict[str, int]:
    """Counts by kind, for logging and the review header."""
    counts = {
        "modified": sum(1 for d in deltas if d.kind == RuleDelta.MODIFIED),
        "added": sum(1 for d in deltas if d.kind == RuleDelta.ADDED),
        "removed": sum(1 for d in deltas if d.kind == RuleDelta.REMOVED),
    }
    # ``total`` counts what a reviewer would see, so unchanged pairs are
    # excluded even when the caller asked for them.
    counts["total"] = sum(counts.values())
    return counts


def _jsonable(fields: dict[str, Any]) -> dict[str, Any]:
    """Coerce decision field values into JSON-safe primitives."""
    out: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if isinstance(value, bool) or value is None:
            out[key] = value
        elif isinstance(value, (int, float, str)):
            out[key] = value
        else:
            out[key] = str(value)
    return out
