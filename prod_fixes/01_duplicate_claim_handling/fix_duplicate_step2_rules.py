#!/usr/bin/env python3
r"""Repair Duplicate Verification (Step 2) so its claim criteria are EVALUATED,
not skipped, and the step routes correctly.

Background
----------
Step 2 of the "OBH Facets Duplicate Claim Handling" SOP asks whether the claim
criteria match a potential duplicate:

    Claim Criteria: Provider NPI/TIN, Member, Date of Service (DOS),
                    Procedure (excluding modifier(s))
    • All items match:            Skip to Step 4.
    • Any item/items do not match: Proceed to the next step.

For the SOP variants actually bound into the workflow (6, 8, 9), ingestion
collapsed Step 2 to a single non-evaluable row (condition "(applies to this
step)", action "Claim Criteria: Provider NPI or TIN") with NO routing. So the
engine never checks Member/DOS/Procedure and never applies the All-match /
Any-mismatch routing.

What this does
--------------
For every DUPLICATE SOP whose Step 2 is bound to a canvas shape, it rewrites
that step to match the SOP exactly — a SINGLE criteria determination with two
complementary routing outcomes (NOT one row per criterion):

    row0  All items match (Provider NPI/TIN, Member, DOS, Procedure excl.
          modifiers all match the potential duplicate) -> goto Step 4
    row1  Any item/items do not match (incl. no matching duplicate found)
          -> goto Step 3 (proceed to next step)

Each row's condition enumerates all four criteria, so the LLM evaluates them
together in one call rather than as fragmented, independently-reasoned rows.

It also SHARES the duplicate-claim tool across the step: any tool bound to a
specific Step 2 row (typically ``facets_get_duplicate_claim`` on the old row 0)
is detached to SHAPE scope so BOTH routing rows see the same duplicate-candidate
evidence. Otherwise only the row it was bound to sees the tool output and the
other row reasons blind ("no reference claim").

Idempotent: re-running rewrites the same two rows in place and re-detaches the
tool binding (a no-op once shape-scoped).

DB target = hardcoded PROD Postgres by default (see HARDCODED_PG). Any PG_* env
var overrides it, so run against local by exporting PG_HOST/PG_PORT/etc. The
banner ALWAYS prints the resolved target first; use --dry-run to preview and
--apply to commit.

    # prod (bare run uses the hardcoded creds)
    python scripts/fix_duplicate_step2_rules.py --dry-run
    python scripts/fix_duplicate_step2_rules.py --apply

    # local (override with env)
    PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres PG_PASSWORD=postgres \
    PG_DATABASE=uhc_backend python scripts/fix_duplicate_step2_rules.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_repo_root(start: str) -> str:
    d = start
    for _ in range(6):
        if os.path.exists(os.path.join(d, "manage.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return start


REPO_ROOT = _find_repo_root(_HERE)

# ── Hardcoded PROD Postgres defaults (env vars still override via setdefault) ──
# So a bare `python scripts/fix_duplicate_step2_rules.py` on the prod box hits
# prod. To target a DIFFERENT DB (e.g. local on 5433), set PG_* env vars, which
# win over these. The banner always prints the resolved target before writing.
HARDCODED_PG = {
    "PG_HOST": "azure-pgsql-flexibleserver-np-390744103630-dev.privatelink.postgres.database.azure.com",
    "PG_PORT": "5432",
    "PG_USER": "pgazdev",
    "PG_PASSWORD": "Xudzab-doxsoz-1vudra",
    "PG_DATABASE": "uhc_backend",
}

STEP_NUMBER = 2
QUESTION = "Determine if the claim criteria listed below matches the current claim."
INTRO_TEXT = (
    "Actions:\n"
    "- Claim Criteria:\n"
    "- Provider NPI or Provider Tax Identification Number (TIN).\n"
    "- Member.\n"
    "- Date of Service (DOS)\n"
    "- Procedure (excluding modifier(s))\n"
    "- All items match: Skip to Step 4.\n"
    "- Any item/items do not match: Proceed to the next step."
)

# Step 2 is a SINGLE determination in the SOP: compare the four claim criteria
# (Provider NPI/TIN, Member, DOS, Procedure excluding modifiers) against the
# potential duplicate claim, then route:
#     • All items match            -> Skip to Step 4
#     • Any item/items do not match -> Proceed to the next step
# We model it as exactly those TWO complementary outcomes (not one decision row
# per criterion). Each row's condition enumerates all four criteria, so the LLM
# evaluates them together in ONE call using the shared duplicate-claim evidence —
# no fragmented, contradictory per-criterion evaluations.
# (subrule_id, condition, action, goto_step)
ROWS = [
    ("RULE-002-001",
     "All items match: the potential duplicate claim matches the billed claim "
     "on ALL of the following — Provider NPI or Provider Tax Identification "
     "Number (TIN), Member, Date of Service (DOS), and Procedure (excluding "
     "modifier(s)).",
     "Skip to Step 4.", 4),
    ("RULE-002-002",
     "Any item/items do not match: one or more of Provider NPI or Provider Tax "
     "Identification Number (TIN), Member, Date of Service (DOS), or Procedure "
     "(excluding modifier(s)) does not match the potential duplicate claim — "
     "including the case where no matching potential duplicate claim is found.",
     "Proceed to the next step.", 3),
]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fix Duplicate Verification Step 2 rules in the DB."
    )
    ap.add_argument("--title-like", default="duplicate",
                    help="ILIKE token matching AuditSop.title (default: duplicate).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview only; write nothing (default if neither flag).")
    ap.add_argument("--apply", action="store_true",
                    help="Commit the changes.")
    opts = ap.parse_args()
    dry = not opts.apply

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")
    # Bake in prod DB creds before Django reads settings; env vars still win.
    for key, val in HARDCODED_PG.items():
        os.environ.setdefault(key, val)

    import django

    django.setup()

    from django.conf import settings
    from django.db import transaction

    from agent_tools.models import NodeRuleBinding, NodeToolBinding
    from sop_ingestion.models import AuditDecision, AuditSop, AuditStep

    db = settings.DATABASES["default"]
    print("── Target DB ───────────────────────────────────────────────")
    print(f"  HOST = {db.get('HOST')}  PORT = {db.get('PORT')}")
    print(f"  NAME = {db.get('NAME')}  USER = {db.get('USER')}")
    print(f"  mode = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    print("────────────────────────────────────────────────────────────")

    dup_sop_ids = list(
        AuditSop.objects.filter(title__icontains=opts.title_like)
        .values_list("id", flat=True)
    )
    if not dup_sop_ids:
        sys.exit(f"No AuditSop matched title~{opts.title_like!r}.")

    # Only the SOP steps actually bound to a shape are executed. Find the
    # (sop_id, shape_id) pairs whose Step 2 is bound.
    bound = (
        NodeRuleBinding.objects
        .filter(sop_id__in=dup_sop_ids, rule_key__contains=f":{STEP_NUMBER}:")
        .values_list("sop_id", "shape_id", "rule_key")
    )
    targets: dict[int, set] = {}
    for sop_id, shape_id, rule_key in bound:
        parts = rule_key.split(":")
        # rule_key == step:<sop>:<step_number>:<row_index>
        if len(parts) == 4 and parts[0] == "step" and parts[2] == str(STEP_NUMBER):
            targets.setdefault(sop_id, set()).add(str(shape_id))

    if not targets:
        sys.exit(
            f"No bound Step {STEP_NUMBER} rules found on duplicate SOPs "
            f"{sorted(dup_sop_ids)} — nothing is executed, nothing to fix."
        )

    print(f"Duplicate SOPs with a bound Step {STEP_NUMBER}: "
          f"{sorted(targets)}\n")

    total_dec = 0
    total_bind = 0
    total_tool = 0

    def _do() -> None:
        nonlocal total_dec, total_bind, total_tool
        for sop_id in sorted(targets):
            step = (AuditStep.objects
                    .filter(sop_id=sop_id, step_number=STEP_NUMBER).first())
            if step is None:
                print(f"  [SKIP] sop_id={sop_id}: no Step {STEP_NUMBER} row.")
                continue
            shapes = sorted(targets[sop_id])
            print(f"  SOP {sop_id}  step_id={step.id}  shapes={shapes}")

            step.question = QUESTION
            step.intro_text = INTRO_TEXT
            if not dry:
                step.save(update_fields=["question", "intro_text"])

            # ── decision rows (source IR) ──
            for i, (sid, cond, act, goto) in enumerate(ROWS):
                defaults = dict(
                    subrule_id=sid,
                    table_name="Step 2 Action Table",
                    depth=0,
                    aggregation="LEAF",
                    applicable_when="",
                    condition_if=cond,
                    condition_and="",
                    action_text=act,
                    action_summary=act,
                    output_text="",
                    decision_type="CONDITIONAL",
                    goto_step=goto,
                    is_final=False,
                    is_out_of_scope=False,
                    tooling_allowed=True,
                    eob_codes=[], ex_codes=[], denial_codes=[],
                    system_actions=[], all_codes=[],
                )
                print(f"      row{i} [{sid}] goto={goto}  {cond[:60]}…")
                if not dry:
                    AuditDecision.objects.update_or_create(
                        step=step, row_index=i, defaults=defaults)
                total_dec += 1
            # Drop any stale rows beyond our six.
            stale = AuditDecision.objects.filter(
                step=step, row_index__gte=len(ROWS))
            n_stale = stale.count()
            if n_stale:
                print(f"      (removing {n_stale} stale decision row(s))")
                if not dry:
                    stale.delete()

            # ── bindings (what the engine executes) ──
            for shape_id in shapes:
                for i, (sid, cond, act, goto) in enumerate(ROWS):
                    rule_key = f"step:{sop_id}:{STEP_NUMBER}:{i}"
                    if not dry:
                        NodeRuleBinding.objects.update_or_create(
                            shape_id=shape_id, rule_key=rule_key,
                            defaults=dict(
                                sop_id=sop_id, condition=cond,
                                action=act, ordering=i),
                        )
                    total_bind += 1
                stale_b = NodeRuleBinding.objects.filter(
                    shape_id=shape_id, sop_id=sop_id,
                    rule_key__startswith=f"step:{sop_id}:{STEP_NUMBER}:",
                ).exclude(rule_key__in=[
                    f"step:{sop_id}:{STEP_NUMBER}:{i}" for i in range(len(ROWS))
                ])
                n_sb = stale_b.count()
                if n_sb:
                    print(f"      (removing {n_sb} stale binding(s) on "
                          f"shape {shape_id})")
                    if not dry:
                        stale_b.delete()

                # ── share tool evidence across the whole step ──
                # Any tool bound to a SPECIFIC Step 2 row (e.g.
                # facets_get_duplicate_claim tied to row 0) is detached to SHAPE
                # scope so every one of Step 2's rows sees the same duplicate
                # candidate result. Without this only the row it was bound to
                # sees the tool output and the other rows reason blind.
                scoped_tools = NodeToolBinding.objects.filter(
                    shape_id=shape_id,
                    rule_binding__rule_key__startswith=f"step:{sop_id}:{STEP_NUMBER}:",
                ).select_related("tool", "rule_binding")
                for tb in scoped_tools:
                    print(f"      detaching tool '{tb.tool.name}' from "
                          f"{tb.rule_binding.rule_key} -> shape-scoped "
                          f"(shared across Step {STEP_NUMBER})")
                    if not dry:
                        tb.rule_binding = None
                        tb.save(update_fields=["rule_binding"])
                    total_tool += 1

    if dry:
        _do()
        print(f"\nDRY-RUN: would write {total_dec} decision row(s), "
              f"{total_bind} binding(s), and detach {total_tool} tool "
              f"binding(s) to shape scope. Re-run with --apply to commit.")
    else:
        with transaction.atomic():
            _do()
        print(f"\nAPPLIED: {total_dec} decision row(s), {total_bind} "
              f"binding(s) written, {total_tool} tool binding(s) shared "
              f"across the step.")


if __name__ == "__main__":
    main()
