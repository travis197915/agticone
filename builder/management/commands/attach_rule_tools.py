"""
Attach SOP rule -> tool mappings onto an existing workflow's canvas shapes.

This is the in-repo equivalent of the external "SOP Rule-to-Tool Mapping
Report": it materialises a rule->tool matrix as **rule-scoped** tool
bindings, i.e. ``NodeToolBinding.rule_binding -> NodeRuleBinding``.

It is deliberately built on top of the existing source-of-truth path:
it writes ``Shape.properties.tool_calls`` entries (each carrying the
rule's ``rule_key``) and then runs the standard
``builder.bindings_sync.extract_bindings_from_properties`` projection.
That keeps it 100%% backward-compatible:

* the same rows a normal canvas PUT would create are produced;
* a later GET round-trips them via ``hydrate_properties_with_bindings``;
* the execution engine sees them in ``rule_loader.tools_by_rule_key``.

Usage::

    python manage.py attach_rule_tools \
        --workflow <workflow-uuid> \
        --map yaml/duplicate_verification_tool_map.yaml [--dry-run] [--replace]

Resolution precedence per rule (by ``subrule_id``): exact > family-prefix
> default (see the map YAML for the schema).
"""
from __future__ import annotations

import yaml
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from agent_tools.models import Tool
from builder.bindings_sync import extract_bindings_from_properties
from builder.models import Shape, Workflow


class Command(BaseCommand):
    help = "Attach rule-scoped tool bindings to a workflow from a rule->tool map YAML."

    def add_arguments(self, parser):
        parser.add_argument("--workflow", required=True,
                            help="Workflow UUID whose shapes carry the SOP rules.")
        parser.add_argument("--map", required=True, dest="map_path",
                            help="Path to the rule->tool mapping YAML.")
        parser.add_argument("--sop-id", type=int, default=None,
                            help="Only map rules belonging to this SOP id.")
        parser.add_argument("--replace", action="store_true",
                            help="Drop previously managed rule-scoped tool_calls "
                                 "before re-attaching (clean re-run).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change without writing.")

    # ── mapping resolution ──────────────────────────────────────────────
    def _resolve_tools(self, subrule_id, spec):
        """Return the list of tool names for a subrule_id (exact > family > default)."""
        sid = subrule_id or ""
        exact = spec.get("exact") or {}
        if sid in exact:
            return list(exact[sid] or [])
        families = spec.get("families") or {}
        best_key = None
        for fam in families:
            if sid == fam or sid.startswith(fam + "-"):
                if best_key is None or len(fam) > len(best_key):
                    best_key = fam
        if best_key is not None:
            return list(families[best_key] or [])
        return list(spec.get("default_tools") or [])

    def handle(self, *args, **opts):
        try:
            with open(opts["map_path"], "r", encoding="utf-8") as fh:
                spec = yaml.safe_load(fh) or {}
        except OSError as exc:
            raise CommandError(f"Cannot read map file: {exc}") from exc

        try:
            workflow = Workflow.objects.get(id=opts["workflow"])
        except Workflow.DoesNotExist as exc:
            raise CommandError(f"Workflow {opts['workflow']} not found.") from exc

        # Pre-resolve + cache every tool name referenced anywhere in the spec.
        referenced: set[str] = set(spec.get("default_tools") or [])
        for v in (spec.get("families") or {}).values():
            referenced.update(v or [])
        for v in (spec.get("exact") or {}).values():
            referenced.update(v or [])
        tool_by_name: dict[str, Tool] = {}
        missing: list[str] = []
        for name in sorted(referenced):
            t = Tool.objects.filter(name=name).first()
            if t is None:
                missing.append(name)
            else:
                tool_by_name[name] = t
        if missing:
            raise CommandError(
                "Unknown tool name(s) in map (not in agent_tools.Tool registry): "
                + ", ".join(missing)
            )

        shapes = list(
            Shape.objects.filter(workbench__work_area__workflow=workflow)
        )
        if not shapes:
            raise CommandError("Workflow has no shapes.")

        dry = opts["dry_run"]
        replace = opts["replace"]
        sop_filter = opts["sop_id"]

        totals = {"rules": 0, "attached": 0, "no_tool": 0, "skipped_existing": 0}
        per_tool: dict[str, int] = {}
        report_lines: list[str] = []

        for shape in shapes:
            props = dict(shape.properties or {})
            rules = props.get("sop_rules") or []
            tool_calls = list(props.get("tool_calls") or [])

            # Index existing (rule_key, tool_name) so re-runs are idempotent.
            def _name_of(tc):
                return tc.get("name") or tc.get("tool_name") or ""
            existing_pairs = {
                (tc.get("rule_key"), _name_of(tc))
                for tc in tool_calls
                if isinstance(tc, dict)
            }

            if replace:
                managed_keys = {
                    r.get("key") for r in rules
                    if isinstance(r, dict) and r.get("key")
                }
                before = len(tool_calls)
                tool_calls = [
                    tc for tc in tool_calls
                    if not (isinstance(tc, dict) and tc.get("rule_key") in managed_keys)
                ]
                existing_pairs = {
                    (tc.get("rule_key"), _name_of(tc))
                    for tc in tool_calls if isinstance(tc, dict)
                }
                if before != len(tool_calls):
                    report_lines.append(
                        f"  [{shape.label[:24]}] dropped {before - len(tool_calls)} "
                        f"prior managed tool_calls"
                    )

            shape_changed = False
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                rule_key = rule.get("key")
                if not rule_key:
                    continue
                if sop_filter is not None and rule.get("sop_id") != sop_filter:
                    continue
                totals["rules"] += 1
                sid = rule.get("subrule_id") or ""
                names = self._resolve_tools(sid, spec)
                if not names:
                    totals["no_tool"] += 1
                    continue
                for name in names:
                    if (rule_key, name) in existing_pairs:
                        totals["skipped_existing"] += 1
                        continue
                    tool = tool_by_name[name]
                    tool_calls.append({
                        "tool_id": str(tool.id),
                        "name": tool.name,
                        "args_template": {},
                        "rule_key": rule_key,
                    })
                    existing_pairs.add((rule_key, name))
                    shape_changed = True
                    totals["attached"] += 1
                    per_tool[name] = per_tool.get(name, 0) + 1

            if shape_changed and not dry:
                props["tool_calls"] = tool_calls
                shape.properties = props
                shape.save(update_fields=["properties"])
                extract_bindings_from_properties(shape)

        # ── report ──────────────────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Rule->tool mapping for workflow {workflow.id} "
            f"({'DRY-RUN' if dry else 'APPLIED'})"
        ))
        for line in report_lines:
            self.stdout.write(line)
        self.stdout.write(f"  rules scanned        : {totals['rules']}")
        self.stdout.write(f"  tool bindings added  : {totals['attached']}")
        self.stdout.write(f"  already present      : {totals['skipped_existing']}")
        self.stdout.write(f"  rules with no tool   : {totals['no_tool']}")
        if per_tool:
            self.stdout.write("  per-tool counts:")
            for name, n in sorted(per_tool.items()):
                self.stdout.write(f"    - {name}: {n}")
        if dry:
            self.stdout.write(self.style.WARNING("Dry-run: nothing written."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
