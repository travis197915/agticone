"""Map every rule in a workflow to the captured tool call(s) that feed it.

This wires the workflow so it depends on **only** the tool calls captured in a
claim snapshot (``claimid_*.json``) — the "18 tool calls" universe — and nothing
else. It:

1. Registers the captured tool-call names as :class:`agent_tools.Tool` rows
   (so a binding can FK to them and the offline runner can seed each by name).
2. Parses the ``SOP_Rule-to-Tool_Mapping_Report`` .docx into
   ``{yaml_basename: {rule_id: [report_tool_names]}}`` (resolving "same as"
   back-references).
3. For every ``NodeRuleBinding`` in the workflow, looks up its
   ``AuditDecision.subrule_id`` (== the report's ``RULE-xxx`` id), resolves the
   report tool name(s) to the captured-call universe, and stores rule-scoped
   ``NodeToolBinding`` rows.
4. Drops any pre-existing tool binding on the workflow that does not point at a
   captured call, so the workflow truly uses *only* the captured tools.

Usage::

    PYTHONPATH=. python manage.py map_rules_to_captured_tools \
        --workflow ae663a90-4e59-4bdc-9648-8b733ed162e0 \
        --report "SOP_Rule-to-Tool_Mapping_Report (1).docx" \
        --snapshot claimid_2026-06-06T18-10-33-811Z.json
"""
from __future__ import annotations

import json
import re
import zipfile
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from agent_tools.models import NodeRuleBinding, NodeToolBinding, Tool
from builder.models import Workflow
from sop_ingestion.models import AuditDecision

# ── report tool-name  ->  captured-call name ────────────────────────────────
# The mapping report uses the upstream (thynkr) tool names; the claim snapshot
# captured a slightly different, canonical set. This table projects every report
# name onto the captured universe. ``None`` == the captured snapshot has no
# equivalent call, so the rule gets no tool (honest gap, not a guess).
REPORT_TO_CAPTURED: dict[str, str | None] = {
    "facets_get_summary": "facets_get_summary",
    "facets_claim_summary": "facets_get_claim_summary",
    "facets_get_claim_summary": "facets_get_claim_summary",
    "facets_get_member_eligibility": "facets_get_member_eligibility",
    "facets_get_member_info": "facets_get_member_info",
    "facets_get_line_details": "facets_get_line_details",
    "get_claim_line_details": "facets_get_line_details",
    "facets_get_duplicate_claim": "facets_get_duplicate_claim",
    "facet_ext_portal_group_model": "facet_ext_portal_group_model",
    "doc360": "doc360_read_claim_by_fln_dcc",
    "doc360_read_claim_by_fln_dcc": "doc360_read_claim_by_fln_dcc",
    "claim_micro_image_id_to_fln_dcc_doc360_parse": "doc360_read_claim_by_fln_dcc",
    "npi_registry": "npi_registry_lookup",
    "npi_registry_lookup": "npi_registry_lookup",
    "diagnosis_coverage_tool": "cbd_coverage",
    "check_member_address_and_state": "check_member_address_and_state",
    "check_timely_filing_state_date": "check_timely_filing_state_date",
    "check_timely_filing_deadline": "check_timely_filing_deadline",
    # No captured equivalent in the snapshot's 18 calls:
    "facets_get_provider_details": None,
    "medicare_optout": None,
    "medicare_optout_tool": None,
    "calculate_days_between_dates": None,
    "facet_ext_portal_group_model ": "facet_ext_portal_group_model",
}

_RULE_ID_RE = re.compile(r"RULE[-_][0-9][0-9\-]*", re.IGNORECASE)
_NO_TOOL_RE = re.compile(r"no (additional )?tool used|not specified", re.IGNORECASE)


def _clean_tool(tok: str) -> str:
    return tok.strip().strip("`").strip()


def _docx_paragraphs(path: str) -> list[str]:
    z = zipfile.ZipFile(path)
    doc = next((n for n in z.namelist()
                if re.fullmatch(r"word/document\d*\.xml", n)), None)
    if doc is None:
        raise CommandError(f"no word/document.xml in {path}")
    xml = z.read(doc).decode("utf-8", "ignore")
    out = []
    for para in re.split(r"</w:p>", xml):
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para)).strip()
        if text:
            out.append(text)
    return out


def parse_report(path: str) -> dict[str, dict[str, list[str]]]:
    """``{yaml_basename: {RULE-id: [report_tool_names]}}`` from the report docx."""
    by_yaml: dict[str, dict[str, list[str]]] = {}
    cur_yaml: str | None = None
    raw_cells: dict[str, dict[str, str]] = {}

    for line in _docx_paragraphs(path):
        m_yaml = re.search(r"([\w. ]+\.yaml)", line)
        if line.lstrip().startswith("-") and m_yaml:
            cur_yaml = m_yaml.group(1).split("\\")[-1].split("/")[-1].strip()
            by_yaml.setdefault(cur_yaml, {})
            raw_cells.setdefault(cur_yaml, {})
            continue
        if not line.startswith("|") or cur_yaml is None:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        rule_cell, tool_cell = cells[0], cells[1]
        m_rule = _RULE_ID_RE.search(rule_cell)
        if not m_rule:
            continue
        rule_id = m_rule.group(0).upper().replace("_", "-")
        raw_cells[cur_yaml][rule_id] = tool_cell

    # Resolve the raw tool cells (handles "same as RULE-xxx" back-references).
    for yaml_name, rules in raw_cells.items():
        resolved: dict[str, list[str]] = {}

        def _resolve(rid: str, seen: set[str]) -> list[str]:
            if rid in resolved:
                return resolved[rid]
            cell = rules.get(rid, "")
            if "same as" in cell.lower():
                refs = [r.upper().replace("_", "-") for r in _RULE_ID_RE.findall(cell)]
                target = next((r for r in refs if r != rid), None)
                if target and target not in seen:
                    return _resolve(target, seen | {rid})
                return []
            if _NO_TOOL_RE.search(cell) or not cell:
                return []
            tools = [_clean_tool(t) for t in cell.split(",")]
            return [t for t in tools if t and not _NO_TOOL_RE.search(t)]

        for rid in rules:
            resolved[rid] = _resolve(rid, set())
        by_yaml[yaml_name] = resolved
    return by_yaml


def _yaml_of_binding(rb: NodeRuleBinding) -> str:
    """Extract the SOP's yaml basename from the workbench header label."""
    name = rb.shape.workbench.name if rb.shape and rb.shape.workbench else ""
    m = re.search(r"([\w. ]+\.yaml)", name)
    return m.group(1).split("/")[-1].strip() if m else ""


def _decision_for_key(rule_key: str) -> AuditDecision | None:
    parts = rule_key.split(":")
    if len(parts) != 4 or parts[0] != "step":
        return None
    _, sop_id, step_no, row_idx = parts
    try:
        return (AuditDecision.objects
                .filter(step__sop_id=sop_id, step__step_number=int(step_no),
                        row_index=int(row_idx))
                .select_related("step").first())
    except (ValueError, TypeError):
        return None


class Command(BaseCommand):
    help = "Map workflow rules to captured tool calls and persist rule-scoped bindings."

    def add_arguments(self, parser):
        parser.add_argument("--workflow", required=True)
        parser.add_argument("--report", required=True, help="path to mapping .docx")
        parser.add_argument("--snapshot", required=True, help="path to claimid_*.json")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        wf_id = opts["workflow"]
        try:
            wf = Workflow.objects.get(id=wf_id)
        except Workflow.DoesNotExist:
            raise CommandError(f"workflow {wf_id} not found")

        snapshot = json.load(open(opts["snapshot"]))
        captured = [tc.get("tool_call_name", "") for tc in snapshot.get("tool_calls", [])]
        captured = [c for c in captured if c]
        captured_set = set(captured)
        self.stdout.write(f"Captured calls ({len(captured)}): {captured}")

        report = parse_report(opts["report"])
        self.stdout.write(f"Report agents parsed: {sorted(report)}")

        # 1) Register every captured call as a Tool (idempotent).
        tools_by_name: dict[str, Tool] = {}
        created_tools = 0
        for name in captured:
            tool, made = Tool.objects.get_or_create(
                name=name,
                defaults=dict(
                    display_name=name.replace("_", " ").title(),
                    kind=Tool.KIND_LANGCHAIN,
                    invoke_url=f"/api/agent-tools/{name}/invoke",
                    description="Captured claim-snapshot tool call.",
                    metadata={"captured_only": True, "claim_arg": "claim_number"},
                    is_active=True,
                ),
            )
            tools_by_name[name] = tool
            created_tools += int(made)
        self.stdout.write(f"Tools: {len(tools_by_name)} present ({created_tools} newly created)")

        rule_bindings = list(
            NodeRuleBinding.objects
            .filter(shape__workbench__work_area__workflow_id=wf_id)
            .select_related("shape", "shape__workbench", "sop")
        )

        # Resolve every rule -> captured tool names.
        plan: list[tuple[NodeRuleBinding, str, list[str], list[str]]] = []
        per_sop = defaultdict(lambda: {"rules": 0, "mapped": 0, "gap_tools": set()})
        for rb in rule_bindings:
            yaml_name = _yaml_of_binding(rb)
            dec = _decision_for_key(rb.rule_key)
            sub = (dec.subrule_id if dec else "") or ""
            sub = sub.upper().replace("_", "-")
            report_tools = report.get(yaml_name, {}).get(sub, [])
            captured_names, gaps = [], []
            for rt in report_tools:
                mapped = REPORT_TO_CAPTURED.get(rt, "__UNKNOWN__")
                if mapped == "__UNKNOWN__":
                    gaps.append(f"{rt}(unknown)")
                elif mapped is None:
                    gaps.append(f"{rt}(no-capture)")
                elif mapped in captured_set:
                    captured_names.append(mapped)
                else:
                    gaps.append(f"{rt}->{mapped}(not-in-snapshot)")
            captured_names = list(dict.fromkeys(captured_names))
            plan.append((rb, sub, captured_names, gaps))
            key = (rb.sop.title if rb.sop else yaml_name)
            per_sop[key]["rules"] += 1
            per_sop[key]["mapped"] += int(bool(captured_names))
            per_sop[key]["gap_tools"].update(gaps)

        total_binds = sum(len(c) for _, _, c, _ in plan)
        self.stdout.write(self.style.WARNING(
            f"\nPlanned: {len(plan)} rules, {total_binds} rule->tool bindings"))
        for sop, agg in per_sop.items():
            gaps = ", ".join(sorted(agg["gap_tools"])) or "-"
            self.stdout.write(
                f"  {sop[:48]:48s} rules={agg['rules']:3d} mapped={agg['mapped']:3d} gaps=[{gaps}]")

        if opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS("\n[dry-run] no DB writes."))
            return

        with transaction.atomic():
            # Drop existing tool bindings on this workflow that are NOT one of the
            # captured calls, plus all rule-scoped ones (rebuilt below).
            existing = NodeToolBinding.objects.filter(
                shape__workbench__work_area__workflow_id=wf_id)
            dropped = 0
            for tb in existing.select_related("tool"):
                if tb.rule_binding_id is not None or tb.tool.name not in captured_set:
                    tb.delete()
                    dropped += 1
            self.stdout.write(f"Dropped {dropped} non-captured / stale tool bindings")

            made = 0
            for rb, sub, captured_names, _gaps in plan:
                for i, name in enumerate(captured_names):
                    _, created = NodeToolBinding.objects.get_or_create(
                        shape=rb.shape, tool=tools_by_name[name], rule_binding=rb,
                        defaults={"ordering": i,
                                  "args_template": {"claim_number": ""}},
                    )
                    made += int(created)
            self.stdout.write(self.style.SUCCESS(
                f"Created {made} rule-scoped tool bindings (only captured calls)."))
