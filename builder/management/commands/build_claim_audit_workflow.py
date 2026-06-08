"""Build the end-to-end "Complete Claim Audit Pipeline" workflow.

This wires the nine hand-authored SOP rule YAMLs into a single builder
``Workflow`` that the execution engine can run a claim through, in a sensible
claim-adjudication order:

    1. Initial Verification      InitialVerificationAgent.yaml        AuditSop #4
    2. Member Eligibility        MemberEligibility.yaml               AuditSop #1
    3. Provider NPI / Name       NpiMatch.yaml                        synthetic
    4. Provider Selection        ProviderSelectionVerification.yaml   AuditSop #5
    5. Provider Opt-Out          ProviderOptoutAuditAgent.yaml        synthetic
    6. Timely Filing             Timely_Filing.yaml                   AuditSop #6
    7. Duplicate Verification    DuplicateVerification.yaml           AuditSop #2
    8. Coverage / Benefit        coverage_benefit.yaml                synthetic
    9. XMED Diagnosis Coverage   XmedDiagnosisCoverage.yaml           synthetic

Only six SOPs were HTML-ingested (the ``poc/`` folders → AuditSop #1..#6); the
other four YAMLs have no ingested SOP, so we create a lightweight *synthetic*
``AuditSop`` container for each so its rules become first-class, bindable rows.

What it does (idempotent, all-or-nothing in one transaction):

  * ensures the four synthetic AuditSops exist (one shared synthetic
    IngestionJob holds them),
  * imports each YAML into its target AuditSop via ``import_sop_yaml``
    (skip with ``--skip-import``),
  * builds one WorkArea → one Workbench per SOP → one Shape per AuditStep,
    binds every AuditDecision row as a ``NodeRuleBinding`` (rule_key
    ``step:<sop>:<step_number>:<row_index>``) via the standard
    ``extract_bindings_from_properties`` projection,
  * chains the shapes (intra-SOP by step, then SOP→SOP) with ShapeConnections,
  * attaches supporting tools: the duplicate SOP via its rule→tool map
    (``duplicate_verification_tool_map.yaml``, rule-scoped), every other SOP
    via shape-scoped tool bindings on each of its steps.

The execution engine (``execute_shapes``) runs each SOP's step cursor in its
own ``step_number`` namespace, in canvas order — so the nine SOPs run in
sequence without step-number collisions.

Usage::

    PYTHONPATH=. python manage.py build_claim_audit_workflow
    PYTHONPATH=. python manage.py build_claim_audit_workflow --replace
    PYTHONPATH=. python manage.py build_claim_audit_workflow --skip-import --skip-tools
"""
from __future__ import annotations

import hashlib
import os

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from agent_tools.models import Tool
from builder.bindings_sync import extract_bindings_from_properties
from builder.models import (Shape, ShapeConnection, ShapeDefinition, WorkArea,
                            Workbench, Workflow)
from sop_ingestion.models import AuditDecision, AuditSop, AuditStep, IngestionJob
from uhc_execution_engine.rule_loader import _hydrate_decision

YAML_DIR = "yaml"

# Synthetic IngestionJob that owns the hand-authored (non-ingested) SOPs.
_SYNTH_JOB_SEED = "https://hand-authored.local/claim-audit-yaml"

# Ordered audit sequence. ``sop_id`` is the existing AuditSop for the five
# HTML-ingested SOPs; ``synthetic_title`` names a container we create for the
# four YAMLs that have no ingested SOP. ``tools`` are shape-scoped supporting
# tools (registered names only); the duplicate SOP instead uses its map YAML.
SEQUENCE = [
    {
        "workbench": "1. Initial Verification",
        "yaml": "InitialVerificationAgent.yaml",
        "sop_id": 4,
        "tools": ["doc360_read_claim_by_fln_dcc",
                  "llm_parse_claim_with_ontology", "facets_get_summary"],
    },
    {
        "workbench": "2. Member Eligibility",
        "yaml": "MemberEligibility.yaml",
        "sop_id": 1,
        "tools": ["facets_get_member_eligibility",
                  "facets_get_line_details", "facets_get_summary"],
    },
    {
        "workbench": "3. Provider NPI / Name Validation",
        "yaml": "NpiMatch.yaml",
        "synthetic_title": "Provider Name and NPI Validation Audit Guidelines",
        "tools": ["facets_get_provider_details", "llm_parse_claim_with_ontology"],
    },
    {
        "workbench": "4. Provider Selection",
        "yaml": "ProviderSelectionVerification.yaml",
        "sop_id": 5,
        "tools": ["facets_get_provider_details",
                  "doc360_read_claim_by_fln_dcc", "facet_ext_portal_group_model"],
    },
    {
        "workbench": "5. Provider Opt-Out",
        "yaml": "ProviderOptoutAuditAgent.yaml",
        "synthetic_title": "Provider Opt-Out Look-Up Audit Guidelines",
        "tools": ["medicare_optout_checker"],
    },
    {
        "workbench": "6. Timely Filing",
        "yaml": "Timely_Filing.yaml",
        "sop_id": 6,
        "tools": ["facets_get_cob", "facets_get_summary",
                  "facets_get_line_details", "facets_get_duplicate_claim",
                  "doc360_read_claim_by_fln_dcc"],
    },
    {
        "workbench": "7. Duplicate Verification",
        "yaml": "DuplicateVerification.yaml",
        "sop_id": 2,
        "tools": [],  # rule-scoped via duplicate_verification_tool_map.yaml
        "tool_map": "duplicate_verification_tool_map.yaml",
    },
    {
        "workbench": "8. Coverage / Benefit",
        "yaml": "coverage_benefit.yaml",
        "synthetic_title": "Access Covered Benefit SOP",
        "tools": ["check_medicare_coverage", "check_diagnosis_coverage"],
    },
    {
        "workbench": "9. XMED Diagnosis Coverage",
        "yaml": "XmedDiagnosisCoverage.yaml",
        "synthetic_title": "XMED Behavioral Health Diagnosis Coverage Verification",
        "tools": ["check_diagnosis_coverage"],
    },
]

# Canvas geometry
_COL_W = 360.0
_ROW_H = 130.0
_SHAPE_W = 240.0
_SHAPE_H = 90.0


class Command(BaseCommand):
    help = "Build the end-to-end Complete Claim Audit Pipeline workflow."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Complete Claim Audit Pipeline")
        parser.add_argument("--slug", default="complete-claim-audit-pipeline")
        parser.add_argument("--replace", action="store_true",
                            help="Delete any existing workflow with this slug first.")
        parser.add_argument("--skip-import", action="store_true",
                            help="Assume the YAMLs are already imported into their SOPs.")
        parser.add_argument("--skip-tools", action="store_true",
                            help="Build the graph + rule bindings but attach no tools.")

    # ── entry point ────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        self._validate_yaml_paths()
        sequence = self._resolve_sequence()          # fills concrete sop_id

        if not opts["skip_import"]:
            self._import_yamls(sequence)

        with transaction.atomic():
            workflow = self._make_workflow(opts["name"], opts["slug"], opts["replace"])
            built = self._build_graph(workflow, sequence)

        if not opts["skip_tools"]:
            self._attach_tools(workflow, sequence)

        self._report(workflow, built)

    # ── validation ───────────────────────────────────────────────────────────
    def _validate_yaml_paths(self) -> None:
        for entry in SEQUENCE:
            p = os.path.join(YAML_DIR, entry["yaml"])
            if not os.path.isfile(p):
                raise CommandError(f"Missing YAML: {p}")
            tm = entry.get("tool_map")
            if tm and not os.path.isfile(os.path.join(YAML_DIR, tm)):
                raise CommandError(f"Missing tool map YAML: {tm}")

    # ── synthetic SOP containers + sop_id resolution ──────────────────────────
    def _resolve_sequence(self) -> list[dict]:
        job = self._synthetic_job()
        resolved: list[dict] = []
        for entry in SEQUENCE:
            e = dict(entry)
            if e.get("sop_id") is None and e.get("synthetic_title"):
                sop = self._ensure_synthetic_sop(job, e["synthetic_title"], e["yaml"])
                e["sop_id"] = sop.id
            # sanity: the target AuditSop must exist
            if not AuditSop.objects.filter(id=e["sop_id"]).exists():
                raise CommandError(
                    f"Target AuditSop #{e['sop_id']} for {e['yaml']} does not exist.")
            resolved.append(e)
        return resolved

    def _synthetic_job(self) -> IngestionJob:
        job = IngestionJob.objects.filter(seed_url=_SYNTH_JOB_SEED).first()
        if job is None:
            job = IngestionJob.objects.create(
                seed_url=_SYNTH_JOB_SEED, status="DONE",
                max_depth=0, max_docs=0,
            )
        return job

    def _ensure_synthetic_sop(self, job: IngestionJob, title: str,
                              yaml_name: str) -> AuditSop:
        sop = AuditSop.objects.filter(title__iexact=title).first()
        if sop is not None:
            return sop
        chash = hashlib.sha256(title.encode("utf-8")).hexdigest()[:64]
        return AuditSop.objects.create(
            job=job,
            url=f"{_SYNTH_JOB_SEED}/{yaml_name}",
            content_hash=chash,
            doc_format="YAML",
            title=title,
            purpose=f"Hand-authored SOP imported from {yaml_name} (no HTML source).",
        )

    # ── YAML import ────────────────────────────────────────────────────────────
    def _import_yamls(self, sequence: list[dict]) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("Importing SOP YAMLs…"))
        for e in sequence:
            path = os.path.join(YAML_DIR, e["yaml"])
            self.stdout.write(f"  → {e['yaml']} into AuditSop #{e['sop_id']}")
            call_command("import_sop_yaml", path, sop_id=e["sop_id"], verbosity=0)

    # ── workflow shell ─────────────────────────────────────────────────────────
    def _make_workflow(self, name: str, slug: str, replace: bool) -> Workflow:
        slug = slug or slugify(name)
        existing = Workflow.objects.filter(slug=slug).first()
        if existing is not None:
            if not replace:
                raise CommandError(
                    f"Workflow slug {slug!r} already exists ({existing.id}). "
                    "Re-run with --replace to rebuild it.")
            existing.delete()   # cascades areas/benches/shapes/connections/bindings
        return Workflow.objects.create(
            name=name, slug=slug,
            description=("End-to-end claim audit: runs nine SOPs in sequence "
                         "(initial verification → eligibility → provider → "
                         "timely filing → duplicates → coverage)."),
            metadata={"generated_by": "build_claim_audit_workflow"},
        )

    # ── graph build ────────────────────────────────────────────────────────────
    def _build_graph(self, workflow: Workflow, sequence: list[dict]) -> dict:
        rectangle = ShapeDefinition.objects.filter(slug="rectangle").first()
        terminator = ShapeDefinition.objects.filter(slug="round-rectangle").first()
        if rectangle is None:
            raise CommandError("ShapeDefinition 'rectangle' not found — run seed_builder_catalog.")
        terminator = terminator or rectangle

        area = WorkArea.objects.create(
            workflow=workflow, name="Claim Audit", order=0,
            width=_COL_W * len(sequence) + _COL_W, height=_ROW_H * 24,
        )

        stats = {"benches": 0, "shapes": 0, "rules": 0, "skipped_steps": 0}
        # First/last shape per workbench so we can chain SOP→SOP.
        bench_endpoints: list[tuple[Shape | None, Shape | None]] = []

        for col, e in enumerate(sequence):
            sop_id = e["sop_id"]
            sop = AuditSop.objects.get(id=sop_id)
            yaml_ref = f"{YAML_DIR}/{e['yaml']}"
            steps = list(AuditStep.objects.filter(sop=sop).order_by("step_number", "id"))

            # Column header carries the YAML reference up top; the description
            # spells out what the column does + every step it runs (no trim).
            short_title = e["workbench"].split(". ", 1)[-1]
            bench_name = f"{e['workbench']}  ·  {yaml_ref}"
            bench_desc = self._workbench_description(
                col_label=e["workbench"], short_title=short_title,
                sop=sop, yaml_ref=yaml_ref, steps=steps)
            bench = Workbench.objects.create(
                work_area=area, name=bench_name, order=col,
                node_key=slugify(short_title),
                description=bench_desc,
                kind="SOP",
                config={
                    "sop_id": sop_id,
                    "sop_title": sop.title,
                    "yaml": e["yaml"],
                    "yaml_ref": yaml_ref,
                    "purpose": sop.purpose or "",
                    "step_count": len(steps),
                    "does": short_title,
                },
                position_x=col * _COL_W, position_y=0,
                width=_SHAPE_W + 80,
            )
            stats["benches"] += 1

            first_shape: Shape | None = None
            prev_shape: Shape | None = None
            for row, step in enumerate(steps):
                decisions = list(
                    AuditDecision.objects.filter(step=step).order_by("row_index", "id"))
                if not decisions:
                    stats["skipped_steps"] += 1
                    continue
                # Full hydrated rule dict per decision row (same shape the
                # builder `/attachable/` picker + engine rule_loader produce),
                # so the inspector renders condition / action / decision_type /
                # narrative / codes — not just the rule id.
                sop_rules = [
                    _hydrate_decision(sop, step, d, "", "") for d in decisions
                ]

                # Full, untruncated step text on the node label; the richer
                # detail (intro/conditions/actions) goes into description +
                # properties so the inspector shows complete context.
                question = (step.question or "").strip()
                label = f"Step {step.step_number}" + (f": {question}" if question else "")
                detail = self._step_detail(sop, yaml_ref, step, decisions)
                shape = Shape.objects.create(
                    workbench=bench,
                    definition=(terminator if step.is_terminal else rectangle),
                    label=label,
                    description=detail,
                    position_x=col * _COL_W,
                    position_y=(row + 1) * _ROW_H,
                    width=_SHAPE_W, height=_SHAPE_H,
                    order=row,
                    properties={
                        "sop_rules": sop_rules,
                        "tool_calls": [],
                        "sop_id": sop_id,
                        "sop_title": sop.title,
                        "yaml_ref": yaml_ref,
                        "step_number": step.step_number,
                        "step_question": question,
                        "step_detail": detail,
                    },
                )
                extract_bindings_from_properties(shape)
                stats["shapes"] += 1
                stats["rules"] += len(sop_rules)

                if prev_shape is not None:
                    ShapeConnection.objects.create(
                        source_shape=prev_shape, target_shape=shape,
                        label="next")
                if first_shape is None:
                    first_shape = shape
                prev_shape = shape

            bench_endpoints.append((first_shape, prev_shape))

        # Chain SOP → SOP: last shape of bench i → first shape of bench i+1.
        for i in range(len(bench_endpoints) - 1):
            _, last = bench_endpoints[i]
            nxt_first, _ = bench_endpoints[i + 1]
            if last is not None and nxt_first is not None:
                ShapeConnection.objects.create(
                    source_shape=last, target_shape=nxt_first, label="then")

        return stats

    # ── text composition (no truncation) ────────────────────────────────────────
    def _workbench_description(self, *, col_label: str, short_title: str,
                               sop: AuditSop, yaml_ref: str,
                               steps: list) -> str:
        """Full column header context: YAML ref, what it does, every step."""
        lines = [
            f"{col_label}",
            f"Source YAML : {yaml_ref}",
            f"SOP         : {sop.title} (AuditSop #{sop.id})",
        ]
        if sop.purpose:
            lines.append(f"Purpose     : {sop.purpose}")
        lines.append(f"What it does: {short_title} — runs {len(steps)} step(s) "
                     f"in this column, in order.")
        lines.append("")
        lines.append("Steps:")
        for step in steps:
            q = (step.question or "").strip().replace("\n", " ")
            lines.append(f"  • Step {step.step_number}: {q}")
        return "\n".join(lines)

    def _step_detail(self, sop: AuditSop, yaml_ref: str, step,
                     decisions: list) -> str:
        """Full, untrimmed detail for one step: question + intro + every rule."""
        parts = [
            f"SOP: {sop.title}  ·  {yaml_ref}",
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

    # ── tool attachment ────────────────────────────────────────────────────────
    def _attach_tools(self, workflow: Workflow, sequence: list[dict]) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("Attaching tools…"))
        # 1) Shape-scoped tools per SOP (skip the duplicate SOP, which uses a map).
        tool_cache: dict[str, Tool] = {}
        for e in sequence:
            names = e.get("tools") or []
            if not names:
                continue
            tools: list[Tool] = []
            for name in names:
                t = tool_cache.get(name) or Tool.objects.filter(name=name).first()
                if t is None:
                    self.stdout.write(self.style.WARNING(
                        f"  ! tool {name!r} not in registry — skipping"))
                    continue
                tool_cache[name] = t
                tools.append(t)
            if not tools:
                continue
            shapes = Shape.objects.filter(
                workbench__work_area__workflow=workflow,
                workbench__config__sop_id=e["sop_id"],
            )
            for shape in shapes:
                props = dict(shape.properties or {})
                props["tool_calls"] = [{
                    "tool_id": str(t.id), "name": t.name, "args_template": {},
                } for t in tools]
                shape.properties = props
                shape.save(update_fields=["properties"])
                extract_bindings_from_properties(shape)
            self.stdout.write(
                f"  → {e['workbench']}: {len(tools)} shape-scoped tool(s)")

        # 2) Duplicate SOP: rule-scoped tools from its map YAML.
        for e in sequence:
            tm = e.get("tool_map")
            if not tm:
                continue
            self.stdout.write(f"  → {e['workbench']}: rule map {tm}")
            call_command(
                "attach_rule_tools",
                workflow=str(workflow.id),
                map_path=os.path.join(YAML_DIR, tm),
                sop_id=e["sop_id"],
                replace=True, verbosity=1,
            )

    # ── report ─────────────────────────────────────────────────────────────────
    def _report(self, workflow: Workflow, stats: dict) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Built workflow {workflow.name!r} ({workflow.id})"))
        self.stdout.write(
            f"  workbenches : {stats['benches']}\n"
            f"  shapes      : {stats['shapes']}\n"
            f"  rule binds  : {stats['rules']}\n"
            f"  empty steps : {stats['skipped_steps']} (no decisions; not bound)")
        self.stdout.write(self.style.SUCCESS(
            f"\nRun a claim through it with the execution engine using workflow "
            f"id {workflow.id}."))
