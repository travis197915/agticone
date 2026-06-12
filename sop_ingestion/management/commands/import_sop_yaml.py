"""Import a hand-authored SOP rule YAML into the relational audit schema.

This loads a ``*.yaml`` rule file (``sop_metadata`` + ``sop_rules``) and writes
it into the existing claims-audit tables so the rules show up everywhere the
HTML-ingested SOPs do (the ``/api/ingest/<job>/sections/`` viewer, the workflow
rule picker, the execution engine):

    sop_rules[i]                       -> AuditStep            (one per step_number)
    sop_rules[i].subrules[*]           -> AuditDecision        (depth 0, parent=NULL)
    ...nested .subrules[*]             -> AuditDecision        (depth 1..N, parent=above)

As of the canonical-IR work the parsing/planning/writing logic no longer lives
here: the command parses the YAML into a :class:`sop_ir.schema.SopIR` and hands
it to the single shared write gate :func:`sop_ir.persist.persist_ir`. That same
gate is used by the HTML/PDF ingestion pipeline, so a YAML-authored SOP and an
ingested SOP produce byte-for-byte identical, routing-complete rows.

Design goals (unchanged):

  * **Flawless capture** — every rule, sub-rule and sub-sub-rule is persisted.
  * **Natural nesting** — preserved via the AuditDecision self-FK.
  * **Out-of-scope marking** — propagated down the subtree.
  * **Idempotent** — re-running wipes and rebuilds an identical tree.

Usage::

    PYTHONPATH=. python manage.py import_sop_yaml yaml/DuplicateVerification.yaml
    PYTHONPATH=. python manage.py import_sop_yaml yaml/DuplicateVerification.yaml --sop-id 2
    PYTHONPATH=. python manage.py import_sop_yaml yaml/DuplicateVerification.yaml --dry-run
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from sop_ingestion.models import AuditSop
from sop_ir.persist import persist_ir, plan_ir
from sop_ir.schema import SopIR
from sop_ir.validate import validate_ir


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
        if not isinstance(doc.get("sop_rules"), list) or not doc["sop_rules"]:
            raise CommandError("YAML must contain a non-empty 'sop_rules' list.")

        ir = SopIR.from_yaml_doc(doc)

        # Surface routing-invariant violations (advisory; never blocks import so
        # pre-existing authoring quirks stay loadable).
        ok, errors = validate_ir(ir)
        for e in errors:
            style = self.style.ERROR if e.startswith("ERROR:") else self.style.WARNING
            self.stdout.write(style("  " + e))
        if not ok:
            self.stdout.write(self.style.WARNING(
                "  IR has routing errors — importing anyway (see above)."))

        sop = self._resolve_sop(ir.metadata.model_dump(), opts.get("sop_id"))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Importing {len(ir.rules)} top-level rules from {os.path.basename(path)} "
            f"into AuditSop #{sop.id} — {sop.title!r}"
        ))

        plan = plan_ir(ir)

        if opts["dry_run"]:
            self._report(plan, dry=True)
            return

        stats = persist_ir(sop, ir, plan=plan, source="yaml_import",
                           validation={"ok": ok, "errors": errors})
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
