#!/usr/bin/env python3
r"""Make the Duplicate SOP AGREE with a system CDD denial (Steps 7 & 8).

Auditor bug (claim 25XK20940100)
--------------------------------
"Process 7. Duplicate Verification > Step 7 IF: If the claim or Claim lines are
denying as a duplicate (Example: CDD). Claim is denying CDD however Agent
reasoning reflects there is no evidence of a CDD edit. I don't see where the
agent agrees the claim is denied correctly for CDD."

Root cause (tool visibility, verified against prod-replica)
-----------------------------------------------------------
Step 7 row 0 (``step:<sop>:7:0``) is the duplicate-DENY premise gate:
    condition: "If the claim or Claim lines are denying as a duplicate. Example: CDD"
    action:    "Allow system to deny duplicate claim with ultra-blue edit CDD…"
The auditor-authored SOP CONTEXT ("PREMISE GATE") tells the agent the premise is
satisfied when the CURRENT line carries the CDD edit on ``CDML_DISALL_EXCD`` (or
a duplicate EOB/EX code E51/F51/003). That field is returned ONLY by the
``facets_get_line_details`` tool.

But NONE of the duplicate-SOP Step 7 / Step 8 shapes have
``facets_get_line_details`` bound — they only carry ``facets_get_summary``,
``facets_get_duplicate_claim`` and ``doc360_read_claim_by_fln_dcc``. So the
premise-gate row evaluated WITHOUT ``CDML_DISALL_EXCD`` in context and truthfully
concluded "no evidence of a CDD edit" → the claim fell through to Step 9 ALLOW
and the agent never agreed the CDD denial was correct. (Other steps that happen
to have line-details available DID read ``CDML_DISALL_EXCD='CDD'`` — hence the
self-contradiction the auditor saw.)

Audit semantics (per the SOP)
-----------------------------
Step 7/8 row 0 reads: *"Allow system to deny duplicate claim with ultra-blue
edit CDD … then proceed to Step 9 (Process the claim)."* That is the auditor
CONFIRMING the system's own CDD denial and letting the claim PROCESS — a CLEAN
audit outcome (the auditor agrees; no auditor-raised defect). It is NOT the
auditor issuing a new denial. Human auditors therefore expect these claims to
come back CLEAN with the reasoning stating the system correctly denied the
duplicate.

The fix (three additive, idempotent parts)
-------------------------------------------
1. TOOL VISIBILITY — bind ``facets_get_line_details`` SHAPE-SCOPED
   (``rule_binding=None``) to every duplicate-SOP Step 7 and Step 8 shape, so
   every premise-gate row sees ``CDML_DISALL_EXCD`` (and the disallow EOB/EX
   codes). This is the real defect and the deterministic fix.

2. CONFIRM-NOT-DENY RECLASSIFY — the Step 7/8 row 0 "allow system to deny
   duplicate (CDD)" row was modelled as ``decision_type=DENY`` with EOB codes
   ``E51/F51``, so matching it forced the aggregator to emit a DEFECT/DENY
   verdict. Per the SOP this row is a SYSTEM-confirm that proceeds to Step 9, so
   we reclassify it to ``decision_type='SYSTEM'`` and clear its adverse EOB/EX
   codes (keeping ``CDD`` as an informational system edit). The aggregator then
   treats a match as non-adverse → CLEAN.

3. AFFIRMATION CLAUSE — append one explicit line to the duplicate SOP's
   auditor-provided ``extra_context`` so that, when the CDD edit IS present, the
   agent AFFIRMATIVELY matches the Step 7/8 row 0, STATES the system correctly
   denied the claim as a duplicate and the audit AGREES, and treats it as a
   CLEAN confirmation that proceeds to Step 9 — NOT a new auditor denial.

After applying, re-run the affected claim(s) with
``rerun_duplicate_verification_prod.py``.

DB target = hardcoded PROD Postgres by default (any PG_* env var overrides). The
banner prints the resolved target first. Use --dry-run to preview, --apply to
commit.

    # prod
    python scripts/fix_duplicate_step7_line_details.py --dry-run
    python scripts/fix_duplicate_step7_line_details.py --apply

    # local replica
    PG_HOST=127.0.0.1 PG_PORT=5433 PG_USER=postgres PG_PASSWORD=postgres \
    PG_DATABASE=uhc_backend python scripts/fix_duplicate_step7_line_details.py --apply
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

HARDCODED_PG = {
    "PG_HOST": "azure-pgsql-flexibleserver-np-390744103630-dev.privatelink.postgres.database.azure.com",
    "PG_PORT": "5432",
    "PG_USER": "pgazdev",
    "PG_PASSWORD": "Xudzab-doxsoz-1vudra",
    "PG_DATABASE": "uhc_backend",
}

TOOL_NAME = "facets_get_line_details"
DUP_TITLE = "duplicate"
STEPS = (7, 8)

# The "allow system to deny duplicate (CDD)" confirm row is always row_index 0 of
# Steps 7 and 8. Reclassify it from an adverse DENY to a non-adverse SYSTEM
# confirmation and keep only the informational system edit code.
CONFIRM_ROW_INDEX = 0
CONFIRM_DECISION_TYPE = "SYSTEM"
CONFIRM_KEEP_CODES = ["CDD"]

# Appended to the duplicate SOP's auditor ``extra_context`` (once). The context
# update is self-correcting: any previously-appended clause (matched by the
# marker phrases below) is stripped before the current clause is appended, so
# re-running always lands the latest wording.
_AFFIRM_MARKER = "CONFIRM THE SYSTEM CDD DENIAL — CLEAN, PROCEED TO STEP 9"
_OLD_MARKERS = (
    _AFFIRM_MARKER,
    "AFFIRM THE DUPLICATE WHEN THE FACETS EDIT IS PRESENT",
)
_AFFIRM_CLAUSE = (
    "\n\n- " + _AFFIRM_MARKER + ". When the CURRENT claim/line already carries "
    "the duplicate edit — i.e. facets_get_line_details shows CDML_DISALL_EXCD = "
    "'CDD' (Definite Duplicate Claim) — the SOP Step 7 / Step 8 row 0 says to "
    "ALLOW THE SYSTEM to deny the duplicate and then PROCEED TO STEP 9 (Process "
    "the claim). This is a CLEAN audit outcome: the audit AGREES the system "
    "correctly denied the claim as a duplicate (CDD); you are NOT raising a new "
    "auditor denial. Do NOT report 'no evidence of a CDD edit'. Instead, MATCH "
    "the Step 7 / Step 8 row 0, STATE PLAINLY that the system correctly denied "
    "this claim as a duplicate (CDD) and the audit agrees, and let the claim "
    "proceed to Step 9 — the verdict stays CLEAN. Always inspect "
    "facets_get_line_details.CDML_DISALL_EXCD before concluding the premise is "
    "absent."
)


def _strip_prior_clause(ec: str) -> str:
    """Remove any previously-appended affirmation block (by earliest marker)."""
    cut = len(ec)
    for marker in _OLD_MARKERS:
        idx = ec.find(marker)
        if idx != -1:
            # Rewind to the start of the bullet ("\n\n- ") that introduces it.
            bullet = ec.rfind("\n\n- ", 0, idx)
            cut = min(cut, bullet if bullet != -1 else idx)
    return ec[:cut].rstrip()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bind facets_get_line_details to duplicate Step 7/8 shapes "
        "and add the CDD-affirmation context clause."
    )
    ap.add_argument(
        "--title-like",
        default=DUP_TITLE,
        help="ILIKE token matching AuditSop.title (default: duplicate).",
    )
    ap.add_argument(
        "--skip-context",
        action="store_true",
        help="Only fix tool bindings; do not touch extra_context.",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Preview only; write nothing (default)."
    )
    ap.add_argument("--apply", action="store_true", help="Commit the changes.")
    opts = ap.parse_args()
    dry = not opts.apply

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")
    for key, val in HARDCODED_PG.items():
        os.environ.setdefault(key, val)

    import django

    django.setup()

    from django.conf import settings
    from django.db import transaction

    from agent_tools.models import NodeRuleBinding, NodeToolBinding, Tool
    from builder.models import Shape, Workbench
    from sop_ingestion.models import AuditDecision, AuditSop, AuditStep

    db = settings.DATABASES["default"]
    print("── Target DB ───────────────────────────────────────────────")
    print(f"  HOST = {db.get('HOST')}  PORT = {db.get('PORT')}")
    print(f"  NAME = {db.get('NAME')}  USER = {db.get('USER')}")
    print(f"  mode = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    print("────────────────────────────────────────────────────────────")

    tool = Tool.objects.filter(name=TOOL_NAME).first()
    if tool is None:
        sys.exit(f"Tool {TOOL_NAME!r} not found — cannot bind.")

    dup_sop_ids = list(
        AuditSop.objects.filter(title__icontains=opts.title_like).values_list(
            "id", flat=True
        )
    )
    if not dup_sop_ids:
        sys.exit(f"No AuditSop matched title~{opts.title_like!r}.")
    print(f"Duplicate SOP ids: {sorted(dup_sop_ids)}")

    # Distinct (sop_id, shape_id) pairs whose Step 7 / Step 8 is bound.
    targets: set[tuple[int, str]] = set()
    for stepno in STEPS:
        for nb in NodeRuleBinding.objects.filter(
            sop_id__in=dup_sop_ids, rule_key__contains=f":{stepno}:"
        ):
            parts = nb.rule_key.split(":")
            if len(parts) == 4 and parts[0] == "step" and parts[2] == str(stepno):
                targets.add((nb.sop_id, str(nb.shape_id)))

    if not targets:
        sys.exit("No bound Step 7/8 duplicate rules found — nothing to fix.")

    n_bound = n_exists = 0

    def _do_bindings() -> None:
        nonlocal n_bound, n_exists
        print(
            f"\nBinding {TOOL_NAME} (shape-scoped) to "
            f"{len(targets)} Step 7/8 shape(s):"
        )
        for sop_id, shape_id in sorted(targets):
            exists = NodeToolBinding.objects.filter(
                shape_id=shape_id, tool=tool, rule_binding__isnull=True
            ).exists()
            if exists:
                n_exists += 1
                print(f"  [have] sop {sop_id} shape {shape_id}")
                continue
            nxt = (
                NodeToolBinding.objects.filter(shape_id=shape_id)
                .order_by("-ordering")
                .values_list("ordering", flat=True)
                .first()
            )
            ordering = (nxt or 0) + 1
            print(f"  [bind] sop {sop_id} shape {shape_id} (ordering={ordering})")
            if not dry:
                NodeToolBinding.objects.create(
                    shape_id=shape_id,
                    tool=tool,
                    rule_binding=None,
                    args_template={},
                    ordering=ordering,
                )
            n_bound += 1

    n_recl = 0

    def _do_reclassify() -> None:
        """Reclassify Step 7/8 row 0 from adverse DENY to a CLEAN SYSTEM confirm.

        Runtime hydrates bound rules straight from ``AuditDecision`` and the
        aggregator flags a matched rule as a DEFECT when its ``decision_type`` is
        adverse OR it carries ``eob_codes``. So both must change for the confirm
        row to resolve CLEAN. We keep ``goto_step`` (proceed to Step 9) intact and
        retain ``CDD`` as an informational system edit."""
        nonlocal n_recl
        _adverse = {"DENY", "STOP", "PEND", "REFER", "REFERRAL", "PENDED"}
        print(
            "\nReclassifying Step 7/8 row 0 'allow system to deny (CDD)' "
            "-> non-adverse SYSTEM confirm:"
        )
        for stepno in STEPS:
            for step in AuditStep.objects.filter(
                sop_id__in=dup_sop_ids, step_number=stepno
            ):
                dec = AuditDecision.objects.filter(
                    step=step, row_index=CONFIRM_ROW_INDEX
                ).first()
                if dec is None:
                    continue
                is_adverse = (dec.decision_type or "").upper() in _adverse or bool(
                    dec.eob_codes or []
                )
                if not is_adverse:
                    # Already non-adverse (routing/clean) — leave it be.
                    print(
                        f"  [skip] sop {step.sop_id} step {stepno} row 0 "
                        f"({dec.decision_type or '-'}, no EOB) already clean"
                    )
                    continue
                print(
                    f"  [recl] sop {step.sop_id} step {stepno} row 0: "
                    f"{dec.decision_type or '-'} eob={dec.eob_codes or []} "
                    f"-> SYSTEM (goto={dec.goto_step})"
                )
                if not dry:
                    dec.decision_type = CONFIRM_DECISION_TYPE
                    dec.eob_codes = []
                    dec.ex_codes = []
                    dec.denial_codes = list(CONFIRM_KEEP_CODES)
                    dec.system_actions = list(CONFIRM_KEEP_CODES)
                    dec.all_codes = list(CONFIRM_KEEP_CODES)
                    dec.save(
                        update_fields=[
                            "decision_type",
                            "eob_codes",
                            "ex_codes",
                            "denial_codes",
                            "system_actions",
                            "all_codes",
                        ]
                    )
                n_recl += 1
        # Mirror onto the canvas shape properties (builder inspector / display).
        for sop_id, shape_id in sorted(targets):
            shp = Shape.objects.filter(id=shape_id).first()
            if shp is None:
                continue
            props = shp.properties if isinstance(shp.properties, dict) else {}
            rules = props.get("sop_rules")
            if not isinstance(rules, list):
                continue
            changed = False
            for r in rules:
                if not isinstance(r, dict):
                    continue
                key = str(r.get("key") or "")
                parts = key.split(":")
                if (
                    len(parts) == 4
                    and parts[0] == "step"
                    and parts[2] in {str(s) for s in STEPS}
                    and parts[3] == str(CONFIRM_ROW_INDEX)
                ):
                    r_adv = (r.get("decision_type") or "").upper() in _adverse or bool(
                        r.get("eob_codes") or []
                    )
                    if r_adv:
                        r["decision_type"] = CONFIRM_DECISION_TYPE
                        r["codes"] = list(CONFIRM_KEEP_CODES)
                        r["eob_codes"] = []
                        changed = True
            if changed and not dry:
                props["sop_rules"] = rules
                shp.properties = props
                shp.save(update_fields=["properties"])

    n_ctx = 0

    def _do_context() -> None:
        nonlocal n_ctx
        if opts.skip_context:
            print("\n(extra_context update skipped by --skip-context)")
            return
        print("\nAppending CDD-affirmation clause to duplicate SOP extra_context:")
        for wb in Workbench.objects.all():
            cfg = wb.config if isinstance(wb.config, dict) else {}
            ec = cfg.get("extra_context")
            if not isinstance(ec, str) or not ec.strip():
                continue
            low = ec.lower()
            if "duplicate claim handling" not in low and "premise gate" not in low:
                continue
            base = _strip_prior_clause(ec)
            new_ec = base + _AFFIRM_CLAUSE
            if new_ec == ec:
                print(f"  [have] workbench {wb.id}")
                continue
            had_old = any(m in ec for m in _OLD_MARKERS)
            print(
                f"  [{'swap' if had_old else 'edit'}] workbench {wb.id} "
                f"(-> {len(new_ec)} chars)"
            )
            if not dry:
                cfg["extra_context"] = new_ec
                wb.config = cfg
                wb.save(update_fields=["config"])
            n_ctx += 1

    if dry:
        _do_bindings()
        _do_reclassify()
        _do_context()
        print(
            f"\nDRY-RUN: would bind {n_bound} tool binding(s) "
            f"({n_exists} already present), reclassify {n_recl} confirm row(s), "
            f"and update {n_ctx} extra_context block(s). Re-run with --apply "
            f"to commit."
        )
    else:
        with transaction.atomic():
            _do_bindings()
            _do_reclassify()
            _do_context()
        print(
            f"\nAPPLIED: {n_bound} tool binding(s) created "
            f"({n_exists} already present); {n_recl} confirm row(s) "
            f"reclassified; {n_ctx} extra_context block(s) updated."
        )
    print(
        "\nNext: re-run affected claim(s) with "
        "scripts/rerun_duplicate_verification_prod.py so the trace/summary "
        "reflect the agreed CDD denial."
    )


if __name__ == "__main__":
    main()
