"""
Data migration: copy existing ``Shape.properties.{sop_rules, tool_calls}``
into the new :class:`agent_tools.NodeRuleBinding` and
:class:`agent_tools.NodeToolBinding` tables.

This is the one-time backfill. After this migration runs:

* All existing rule attachments live in the binding tables and the
  builder graph GET/PUT round-trips through them.
* The original ``Shape.properties.sop_rules`` / ``tool_calls`` blobs are
  left in place for one release as a back-compat mirror; a follow-up
  migration can drop them.
"""
from __future__ import annotations

import re

from django.db import migrations


def _rule_sop_id(rule_key: str):
    m = re.match(r"^(?:pre|step):(\d+):", rule_key or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _resolve_tool(Tool, payload: dict):
    tool_id = payload.get("tool_id") or payload.get("toolId")
    if tool_id:
        try:
            return Tool.objects.filter(id=tool_id).first()
        except Exception:
            pass
    name = payload.get("name") or payload.get("tool_name")
    if name:
        tool = Tool.objects.filter(name=name).first()
        if tool:
            return tool
    endpoint = payload.get("endpoint_id") or payload.get("endpointId")
    if endpoint:
        return Tool.objects.filter(endpoint_id=endpoint).first()
    return None


def _backfill(apps, _schema_editor):
    Shape = apps.get_model("builder", "Shape")
    AuditSop = apps.get_model("sop_ingestion", "AuditSop")
    Tool = apps.get_model("agent_tools", "Tool")
    NodeRuleBinding = apps.get_model("agent_tools", "NodeRuleBinding")
    NodeToolBinding = apps.get_model("agent_tools", "NodeToolBinding")

    valid_sop_ids = set(AuditSop.objects.values_list("id", flat=True))

    rules_created = 0
    tools_created = 0
    for shape in Shape.objects.all().iterator(chunk_size=500):
        props = shape.properties or {}
        rule_payloads = props.get("sop_rules") or []
        tool_payloads = props.get("tool_calls") or []

        rule_by_key: dict[str, object] = {}
        for idx, rule in enumerate(rule_payloads):
            if not isinstance(rule, dict):
                continue
            rule_key = rule.get("key") or ""
            if not rule_key:
                continue
            sop_id = rule.get("sop_id") or _rule_sop_id(rule_key)
            if not sop_id or sop_id not in valid_sop_ids:
                continue
            row, _ = NodeRuleBinding.objects.update_or_create(
                shape=shape, rule_key=rule_key,
                defaults={
                    "sop_id": sop_id,
                    "condition": rule.get("condition", "") or "",
                    "action": rule.get("action", "") or "",
                    "references_json": list(rule.get("references") or []),
                    "excluded_by_json": list(rule.get("excluded_by") or []),
                    "html_reference_json": rule.get("html_reference") or {},
                    "ordering": idx,
                },
            )
            rule_by_key[rule_key] = row
            rules_created += 1

        for idx, payload in enumerate(tool_payloads):
            if not isinstance(payload, dict):
                continue
            tool = _resolve_tool(Tool, payload)
            if tool is None:
                continue
            picked_key = payload.get("rule_key") or payload.get("for_rule_key")
            rule_binding = rule_by_key.get(picked_key) if picked_key else None
            try:
                NodeToolBinding.objects.update_or_create(
                    shape=shape, tool=tool, rule_binding=rule_binding,
                    defaults={
                        "args_template": payload.get("args_template")
                            or payload.get("argsTemplate") or {},
                        "ordering": idx,
                    },
                )
                tools_created += 1
            except Exception:
                continue


def _noop(_apps, _schema_editor):
    return


class Migration(migrations.Migration):

    dependencies = [
        ("agent_tools", "0002_seed_registry"),
        ("builder", "0001_initial"),
        ("sop_ingestion", "0010_sop_exclusion_html_block"),
    ]

    operations = [
        migrations.RunPython(_backfill, _noop),
    ]
