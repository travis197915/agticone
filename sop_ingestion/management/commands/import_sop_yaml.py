"""Import a hand-authored SOP rule YAML into the relational audit schema.

This loads a ``*.yaml`` rule file (``sop_metadata`` + ``sop_rules``) and writes
it into the existing claims-audit tables so the rules show up everywhere the
HTML-ingested SOPs do (the ``/api/ingest/<job>/sections/`` viewer, the workflow
rule picker, the execution engine):

    sop_rules[i]                       -> AuditStep            (one per step_number)
    sop_rules[i].subrules[*]           -> AuditDecision        (depth 0, parent=NULL)
    ...nested .subrules[*]             -> AuditDecision        (depth 1..N, parent=above)

Design goals (per the task brief):

  * **Flawless capture** — every rule, sub-rule and sub-sub-rule is persisted
    with its description, conditions, actions and Met/Not-Met ``output``.
  * **Natural nesting** — the YAML's 0..N nesting is preserved via the
    AuditDecision self-FK (``parent``/``depth``), not flattened.
  * **Out-of-scope marking** — any rule/section whose text says "out of scope"
    or "stop further auditing" is flagged ``is_out_of_scope`` and the flag is
    propagated down its entire subtree.
  * **Idempotent** — re-running wipes the SOP's steps/decisions/references and
    rebuilds an identical tree (keyed on the YAML rule ids).

Usage::

    PYTHONPATH=. python manage.py import_sop_yaml yaml/DuplicateVerification.yaml
    PYTHONPATH=. python manage.py import_sop_yaml yaml/DuplicateVerification.yaml --sop-id 2
    PYTHONPATH=. python manage.py import_sop_yaml yaml/DuplicateVerification.yaml --dry-run
"""
from __future__ import annotations

import os
import re
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from sop_ingestion.models import (
    AuditSop, AuditStep, AuditDecision, AuditReference,
)

# ── out-of-scope detection ────────────────────────────────────────────────────
_OOS_MARKERS = (
    "out of scope",
    "out-of-scope",
    "stop further auditing",
    "stop auditing further",
)

# ── decision classification (kept local so the command has no pipeline dep) ───
_VALID_DECISIONS = {
    "DENY", "ALLOW", "BYPASS", "PEND", "REFER", "SYSTEM", "STOP", "WAIVE",
    "CONDITIONAL",
}


def _classify_decision(text: str) -> str:
    """Map a rule's action/output text to an adjudication disposition.

    CRITICAL: only *terminal dispositions* (DENY/REFER/PEND/STOP/...) may drive
    the claim verdict. Flow-control language — "proceed to next step", "skip to
    step N", "go to step N", "retrieve ...", "call <tool>" — and "out of scope"
    (a clean line-item exclusion, tracked separately via is_out_of_scope) are
    NOT dispositions. Classifying routing as REFER/STOP was producing false
    DEFECT/REFER verdicts on clean claims, so routing now falls through to the
    neutral CONDITIONAL bucket and never outranks ALLOW in the aggregator.
    """
    # Normalize hyphenated/underscored spellings so "out-of-scope" and
    # "out of scope" (and "skip-to" / "skip to") are treated identically.
    t = re.sub(r"[-_]+", " ", (text or "").upper())
    if "CDD" in t:                                    return "DENY"
    if "DENY" in t or "DENIAL" in t or "DENIED" in t: return "DENY"
    if "BYPASS" in t or "OVERRIDE" in t:              return "BYPASS"
    if "PEND" in t:                                   return "PEND"
    if "WAIVE" in t:                                  return "WAIVE"
    if "ALLOW" in t or ("PROCESS" in t and "F3" in t): return "ALLOW"
    # A genuine referral disposition routes the claim to a human reviewer. In
    # this SOP corpus "refer to <SOP / list / table / section>" is a *citation*,
    # not a disposition — so only treat explicit reviewer routing as REFER.
    if "REFERRAL" in t or ("REFER" in t and any(
        kw in t for kw in _REVIEWER_TARGETS)):        return "REFER"
    # A genuine hard stop — but NOT "out of scope" (a clean exclusion / handoff,
    # tracked via is_out_of_scope) and NOT the routing verbs below.
    if "STOP" in t and "OUT OF SCOPE" not in t:       return "STOP"
    # "proceed", "skip to", "go to step", "retrieve", "call <tool>",
    # "refer to <document>" and "out of scope" are routing / data-gathering,
    # not dispositions.
    return "CONDITIONAL"


# Phrases that mark a genuine "send this claim to a human" referral, as opposed
# to a "refer to <document/SOP/list>" citation.
_REVIEWER_TARGETS = (
    "NURSE", "SPECIALIST", "MEDICAL DIRECTOR", "CLINICAL REVIEW", "MANUAL REVIEW",
    "REVIEWER", "ADJUSTER", "REFER THE CLAIM", "REFER FOR", "REFER TO A ",
)


def _extract_codes(text: str) -> dict[str, list[str]]:
    """Pull EOB / EX / denial / system-action codes out of free text.

    Handles the code families that appear in the OBH duplicate-handling SOP:
    EOB/reason codes (E51, F51, F24, F55, W46, W47), EX codes (003, 020, 001),
    denial edits (CDD) and system actions (F3, F4, F5).
    """
    T = (text or "").upper()
    eob, ex, denial, sysact = [], [], [], []

    # EX codes: "EX code 003", "EX code 020/001", "EX code 020 or EX code 001"
    for chunk in re.findall(r"EX\s*(?:CODE\s*)?((?:\d{3})(?:\s*/\s*\d{3})*)", T):
        for n in re.findall(r"\d{3}", chunk):
            ex.append(n)

    # EOB / reason codes: a letter E/F/W followed by exactly two digits
    for c in re.findall(r"\b([EFW]\d{2})\b", T):
        eob.append(c)

    # System actions: standalone F3 / F4 / F5 (e.g. "(F3)", "pend (F5)")
    for c in re.findall(r"\bF([3-5])\b", T):
        sysact.append("F" + c)

    if "CDD" in T:
        denial.append("CDD")

    dedupe = lambda xs: list(dict.fromkeys(xs))
    return {
        "eob": dedupe(eob),
        "ex": dedupe(ex),
        "denial": dedupe(denial),
        "sysact": dedupe(sysact),
    }


def _extract_goto(text: str) -> int | None:
    """Parse 'skip to Step 4', 'proceed to step 9', 'step 8 directly' -> int."""
    t = (text or "").lower()
    m = re.search(r"(?:skip to|proceed to|go to|directly to|jump to)\s+step\s+(\d+)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"step\s+(\d+)\s+directly", t)
    if m:
        return int(m.group(1))
    return None


def _as_text(v: Any, joiner: str = "\n") -> str:
    """Normalize a YAML scalar / list into a single clean string."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        parts = [_as_text(x, joiner) for x in v]
        return joiner.join(p for p in parts if p and p.strip() and p.strip() != "-")
    s = str(v).strip()
    return "" if s == "-" else s


def _as_list(v: Any) -> list[str]:
    """Normalize a YAML scalar / list into a list of clean strings (drops '-')."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            s = _as_text(x)
            if s and s.strip() and s.strip() != "-":
                out.append(s)
        return out
    s = str(v).strip()
    return [] if (not s or s == "-") else [s]


def _is_out_of_scope(*chunks: Any) -> bool:
    blob = " ".join(_as_text(c, " ") for c in chunks).lower()
    return any(m in blob for m in _OOS_MARKERS)


def _infer_aggregation(output: str, has_children: bool) -> str:
    if not has_children:
        return "LEAF"
    o = (output or "").lower()
    if "any pair" in o or "any of the above" in o:
        return "ANY"
    if "only one subrule" in o or "exactly one" in o:
        return "XOR_ONE"
    if "always" in o and "met" in o:
        return "ALWAYS_MET"
    if "first matching" in o:
        return "FIRST_MATCH"
    return "FIRST_MATCH"


def _map_aggregation_rule(v: Any) -> str | None:
    """Map an explicit YAML `aggregation_rule` onto an AGGREGATION_CHOICES value."""
    s = (str(v or "")).strip().lower()
    if not s:
        return None
    if "applicable" in s:           # "applicable_only"
        return "APPLICABLE_ONLY"
    if s in ("any_clean", "any", "any_match"):
        return "ANY"
    if "xor" in s or "exactly_one" in s or "only_one" in s:
        return "XOR_ONE"
    if "first" in s:
        return "FIRST_MATCH"
    if "always" in s:
        return "ALWAYS_MET"
    return None


class Command(BaseCommand):
    help = "Import a hand-authored SOP rule YAML into the relational audit schema."

    def add_arguments(self, parser):
        parser.add_argument("yaml_path", help="Path to the SOP rule YAML file.")
        parser.add_argument("--sop-id", type=int, default=None,
                            help="Target AuditSop id (skips title/source-file match).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Parse + report the tree without writing to the DB.")

    # ── entry point ──────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        try:
            import yaml  # PyYAML
        except ImportError as exc:  # pragma: no cover
            raise CommandError("PyYAML is required: pip install pyyaml") from exc

        path = opts["yaml_path"]
        if not os.path.isfile(path):
            raise CommandError(f"YAML file not found: {path}")

        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)

        if not isinstance(doc, dict):
            raise CommandError("YAML root must be a mapping with sop_metadata + sop_rules.")
        meta = doc.get("sop_metadata") or {}
        rules = doc.get("sop_rules")
        if not isinstance(rules, list) or not rules:
            raise CommandError("YAML must contain a non-empty 'sop_rules' list.")

        sop = self._resolve_sop(meta, opts.get("sop_id"))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Importing {len(rules)} top-level rules from {os.path.basename(path)} "
            f"into AuditSop #{sop.id} — {sop.title!r}"
        ))

        # Build an in-memory plan first so --dry-run can report without writing.
        plan = [self._plan_rule(r, ridx) for ridx, r in enumerate(rules)]

        if opts["dry_run"]:
            self._report(plan, dry=True)
            return

        with transaction.atomic():
            self._wipe(sop)
            stats = {"steps": 0, "decisions": 0, "refs": 0, "oos": 0}
            for p in plan:
                self._write_step(sop, p, stats)
            sop.step_count = stats["steps"]
            sop.decision_count = stats["decisions"]
            sop.save(update_fields=["step_count", "decision_count", "updated_at"])

        self._report(plan, dry=False, stats=stats, sop=sop)

    # ── SOP resolution ─────────────────────────────────────────────────────────
    def _resolve_sop(self, meta: dict, sop_id: int | None) -> AuditSop:
        if sop_id is not None:
            try:
                return AuditSop.objects.get(pk=sop_id)
            except AuditSop.DoesNotExist:
                raise CommandError(f"AuditSop #{sop_id} does not exist.")

        title = (meta.get("document_title") or "").strip()
        source_file = (meta.get("source_file") or "").strip()

        # 1) exact title match
        if title:
            qs = AuditSop.objects.filter(title__iexact=title)
            if qs.count() == 1:
                return qs.first()

        # 2) source-file basename appears in the SOP url/source path
        if source_file:
            base = os.path.splitext(os.path.basename(source_file))[0].lower()
            # the .htm source maps to a poc/<slug>/html/index.html ingest url
            slug = base.replace("obh_facets_", "").replace("_", " ").strip()
            for s in AuditSop.objects.all():
                hay = f"{s.title} {getattr(s, 'url', '')}".lower()
                if slug and slug in hay:
                    return s

        # 3) fuzzy title contains
        if title:
            qs = AuditSop.objects.filter(title__icontains=title.split(" - ")[0])
            if qs.exists():
                return qs.first()

        raise CommandError(
            "Could not resolve a target AuditSop from metadata "
            f"(title={title!r}, source_file={source_file!r}). "
            "Pass --sop-id <id> explicitly."
        )

    # ── wipe (idempotency) ──────────────────────────────────────────────────────
    def _wipe(self, sop: AuditSop) -> None:
        # Deleting steps cascades their AuditDecision rows (incl. nested children).
        AuditStep.objects.filter(sop=sop).delete()
        # Only remove references that carry no step link AND were authored by us.
        AuditReference.objects.filter(sop=sop, ref_type="YAML_RULE").delete()

    # ── planning (pure, recursive) ─────────────────────────────────────────────
    def _plan_rule(self, rule: dict, ridx: int) -> dict:
        rule_id = (rule.get("rule_id") or "").strip()
        step_number = rule.get("step_number")
        try:
            step_number = int(step_number)
        except (TypeError, ValueError):
            step_number = ridx  # fall back to ordering when step_number missing

        description = _as_text(rule.get("description"))
        conditions = _as_list(rule.get("conditions"))
        actions = _as_list(rule.get("actions"))
        output = _as_text(rule.get("output"))
        subrules = rule.get("subrules") or []

        intro_bits = []
        if conditions:
            intro_bits.append("Conditions:\n- " + "\n- ".join(conditions))
        if actions:
            intro_bits.append("Actions:\n- " + "\n- ".join(actions))
        if output:
            intro_bits.append("Output (Met/Not-Met):\n" + output)
        intro_text = "\n\n".join(intro_bits)

        step_oos = _is_out_of_scope(description, conditions, actions, output)
        # A "blank" step has no real evaluation criteria (empty conditions,
        # actions and subrules — e.g. a placeholder rung). When such a step is
        # out of scope it must be SKIPPED and the audit must CONTINUE to the
        # next step, NOT treated as a terminal exclusion that halts the path.
        # We mark it non-final so the execution engine pre-skips it in place.
        is_blank = not conditions and not actions and not subrules
        action_blob = " ".join([description] + actions + [output])
        terminal_action = ""
        is_terminal = False
        m = re.search(r"\(?(F[3-5])\)?\b", (description + " " + " ".join(actions)).upper())
        if m and ("process the claim" in action_blob.lower()
                  or "save the claim" in action_blob.lower()):
            is_terminal = True
            terminal_action = m.group(1)

        children = [
            self._plan_subrule(sr, i, depth=0, parent_oos=step_oos)
            for i, sr in enumerate(subrules)
        ]

        # Explicit step-level routing of the child group. An `aggregation_rule`
        # YAML key (e.g. "applicable_only", "any_clean") — or the mere presence
        # of `applicable_when` on a direct child — forces the direct children's
        # aggregation so the execution engine evaluates only the applicable one.
        agg_rule = _map_aggregation_rule(rule.get("aggregation_rule"))
        if children:
            any_applicable_when = any(c.get("applicable_when") for c in children)
            forced = "APPLICABLE_ONLY" if (agg_rule == "APPLICABLE_ONLY" or any_applicable_when) else agg_rule
            if forced:
                for c in children:
                    c["aggregation"] = forced

        # Leaf top-level rule (no subrules): synthesize ONE decision row so the
        # rule body (codes / output / decision_type) is captured and rendered.
        if not children:
            codes = _extract_codes(action_blob)
            children = [{
                "subrule_id": rule_id,
                "table_name": _as_text(rule.get("section")),
                "depth": 0,
                "row_index": 0,
                "condition_if": "\n".join(conditions) if conditions else "(applies to this step)",
                "condition_and": "",
                "action_text": "\n".join(actions) if actions else description,
                "output_text": output,
                "decision_type": _classify_decision(action_blob),
                "tooling_allowed": bool(rule.get("tooling_allowed", True)),
                "is_out_of_scope": step_oos,
                "goto_step": _extract_goto(action_blob),
                # Blank out-of-scope steps are skip-and-continue (non-final);
                # content-bearing OOS exclusions remain terminal stops.
                "is_final": is_terminal or (step_oos and not is_blank),
                "aggregation": "LEAF",
                "codes": codes,
                "children": [],
                "_synthetic": True,
            }]

        return {
            "rule_id": rule_id,
            "step_number": step_number,
            "question": description,
            "intro_text": intro_text,
            "is_out_of_scope": step_oos,
            "is_terminal": is_terminal,
            "terminal_action": terminal_action,
            "references": _as_list(rule.get("references")),
            "urls": _as_list(rule.get("urls")),
            "children": children,
        }

    def _plan_subrule(self, sr: dict, idx: int, depth: int, parent_oos: bool) -> dict:
        subrule_id = (sr.get("subrule_id") or "").strip()
        description = _as_text(sr.get("description"))
        conditions = _as_list(sr.get("conditions"))
        actions = _as_list(sr.get("actions"))
        output = _as_text(sr.get("output"))
        sub = sr.get("subrules") or []

        own_oos = _is_out_of_scope(description, conditions, actions, output)
        oos = own_oos or parent_oos

        # condition_if = the row label (description); condition_and = detail conds
        condition_if = description or (conditions[0] if conditions else "")
        condition_and = "\n".join(conditions if not description else conditions)
        action_text = "\n".join(actions)

        codes = _extract_codes(" ".join([description] + conditions + actions + [output]))
        has_children = bool(sub)

        children = [
            self._plan_subrule(s, i, depth=depth + 1, parent_oos=oos)
            for i, s in enumerate(sub)
        ]

        return {
            "subrule_id": subrule_id,
            "table_name": _as_text(sr.get("table_name")),
            "depth": depth,
            "row_index": idx,
            "condition_if": condition_if,
            "condition_and": condition_and,
            "action_text": action_text,
            "output_text": output,
            "applicable_when": _as_text(sr.get("applicable_when")),
            "decision_type": _classify_decision(" ".join(actions) + " " + description + " " + output),
            "tooling_allowed": bool(sr.get("tooling_allowed", True)),
            "is_out_of_scope": oos,
            "goto_step": _extract_goto(" ".join(actions) + " " + output),
            "is_final": oos or bool(_extract_goto(" ".join(actions)) is None and "stop" in action_text.lower()),
            "aggregation": _infer_aggregation(output, has_children),
            "codes": codes,
            "children": children,
            "urls": _as_list(sr.get("urls")),
        }

    # ── writing ─────────────────────────────────────────────────────────────────
    def _write_step(self, sop: AuditSop, p: dict, stats: dict) -> None:
        step = AuditStep.objects.create(
            sop=sop,
            step_number=p["step_number"],
            question=p["question"],
            intro_text=p["intro_text"],
            is_terminal=p["is_terminal"],
            terminal_action=p["terminal_action"],
            is_out_of_scope=p["is_out_of_scope"],
            yaml_rule_id=p["rule_id"],
        )
        stats["steps"] += 1
        if p["is_out_of_scope"]:
            stats["oos"] += 1

        # Step-global pre-order ordinal for row_index. This keeps the
        # `step:<sop>:<step_number>:<row_index>` rule_key UNIQUE within a step
        # even though the tree restarts sibling indexes at 0 per parent — so the
        # builder `/attachable/` endpoint and the execution-engine rule_loader
        # (both key on row_index) keep working unchanged with nested rules.
        counter = [0]
        for child in p["children"]:
            self._write_decision(step, child, parent=None, stats=stats, counter=counter)

        # references + urls for this step
        for txt in p["references"]:
            AuditReference.objects.create(
                sop=sop, step=step, ref_text=txt, ref_url="",
                ref_type="YAML_RULE", is_resolved=False,
            )
            stats["refs"] += 1
        for url in p["urls"]:
            AuditReference.objects.create(
                sop=sop, step=step, ref_text="", ref_url=url,
                ref_type="YAML_RULE", is_resolved=True,
            )
            stats["refs"] += 1

    def _write_decision(self, step: AuditStep, d: dict, parent, stats: dict,
                        counter: list) -> None:
        codes = d["codes"]
        all_codes = list(dict.fromkeys(
            codes["eob"] + codes["ex"] + codes["denial"] + codes["sysact"]
        ))
        row_index = counter[0]
        counter[0] += 1
        dec = AuditDecision.objects.create(
            step=step,
            parent=parent,
            depth=d["depth"],
            subrule_id=d.get("subrule_id", ""),
            table_name=d.get("table_name", ""),
            aggregation=d["aggregation"],
            applicable_when=d.get("applicable_when", ""),
            row_index=row_index,
            condition_if=d["condition_if"],
            condition_and=d["condition_and"],
            action_text=d["action_text"],
            output_text=d["output_text"],
            decision_type=d["decision_type"],
            tooling_allowed=d["tooling_allowed"],
            is_out_of_scope=d["is_out_of_scope"],
            goto_step=d["goto_step"],
            is_final=d["is_final"],
            eob_codes=codes["eob"],
            ex_codes=codes["ex"],
            denial_codes=codes["denial"],
            system_actions=codes["sysact"],
            all_codes=all_codes,
        )
        stats["decisions"] += 1
        if d["is_out_of_scope"]:
            stats["oos"] += 1

        for c in d["children"]:
            self._write_decision(step, c, parent=dec, stats=stats, counter=counter)

    # ── reporting ─────────────────────────────────────────────────────────────
    def _count(self, node: dict) -> int:
        return 1 + sum(self._count(c) for c in node.get("children", []))

    def _report(self, plan: list[dict], dry: bool, stats: dict | None = None,
                sop: AuditSop | None = None) -> None:
        total_dec = sum(sum(self._count(c) for c in p["children"]) for p in plan)
        oos_nodes = []

        def walk(node, kind):
            sid = node.get("subrule_id") or node.get("rule_id")
            if node.get("is_out_of_scope"):
                oos_nodes.append(sid)
            for c in node.get("children", []):
                walk(c, "decision")

        for p in plan:
            walk(p, "step")

        self.stdout.write("")
        for p in plan:
            flag = self.style.WARNING(" [OUT OF SCOPE]") if p["is_out_of_scope"] else ""
            direct = len(p["children"])
            deep = sum(self._count(c) for c in p["children"])
            self.stdout.write(
                f"  Step {p['step_number']:>2} {p['rule_id']:<10} "
                f"{direct} direct / {deep} total decisions{flag}"
            )
            for c in p["children"]:
                if c.get("children"):
                    self.stdout.write(
                        f"        └─ {c['subrule_id']:<18} {len(c['children'])} children"
                    )

        self.stdout.write("")
        verb = "WOULD WRITE" if dry else "WROTE"
        self.stdout.write(self.style.SUCCESS(
            f"{verb}: {len(plan)} steps, {total_dec} decisions, "
            f"{len(oos_nodes)} out-of-scope nodes"
        ))
        if oos_nodes:
            self.stdout.write("  Out-of-scope: " + ", ".join(oos_nodes))
        if not dry and stats:
            self.stdout.write(self.style.SUCCESS(
                f"  DB: {stats['steps']} steps, {stats['decisions']} decisions, "
                f"{stats['refs']} references on AuditSop #{sop.id}"
            ))
