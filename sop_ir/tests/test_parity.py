"""HTML-door vs YAML-door routing parity (pure data — no DB, no LLM, no network).

The thesis of this feature: an HTML/PDF-ingested SOP must route *identically* to
a hand-authored YAML SOP. Both doors funnel a ``SopIR`` through the one shared
``sop_ir.persist`` planner. This test proves convergence end-to-end on the
Timely Filing and NpiMatch POCs:

  YAML door:  yaml file        -> SopIR -> plan_ir            (the reference)
  HTML door:  same SOP rendered as enriched_steps (what a07/a11 emit)
                               -> a18.build_draft_ir -> SopIR -> plan_ir

We assert the engine-consumed routing fields (goto_step, aggregation,
is_out_of_scope, is_final, applicable_when) match per step/sub-rule.

If the doors ever diverge, this fails — the regression guard for parity rollout.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sop_ir.plan import plan_ir
from sop_ir.schema import SopIR

REPO_ROOT = Path(__file__).resolve().parents[2]
YAML_DIR = REPO_ROOT / "yaml"

# Engine-consumed routing fields (n_execute_shapes._run_sop_cursor) that MUST
# survive the HTML door identically to the YAML door.
_ROUTING_FIELDS = ("goto_step", "aggregation", "is_out_of_scope", "is_final")


def _load_ir(name: str) -> SopIR:
    with open(YAML_DIR / name, "r", encoding="utf-8") as fh:
        return SopIR.from_yaml_doc(yaml.safe_load(fh))


def _render_enriched_steps(ir: SopIR) -> list[dict]:
    """Render a SopIR into the ``enriched_steps`` shape a07/a11 emit for HTML.

    Deliberately does NOT pre-extract goto into ``skip_to_step`` — the goto verb
    stays in the sub-rule action text, exercising the deterministic text-based
    extraction both doors share."""
    steps = []
    for r in ir.rules:
        steps.append({
            "number": r.step_number,
            "yaml_rule_id": r.rule_id,
            "question": r.description,
            "section": r.section,
            "conditions": list(r.conditions),
            "actions": list(r.actions),
            "output_text": r.output,
            "decision_rows": [{
                "subrule_id": sr.subrule_id,
                "table_name": sr.table_name,
                "condition_if": sr.description,
                "condition_and": "\n".join(sr.conditions),
                "action": "\n".join(sr.actions),
                "output_text": sr.output,
                "applicable_when": sr.applicable_when,
            } for sr in r.subrules],
        })
    return steps


def _flatten(plan: list[dict]) -> dict[tuple, dict]:
    """Map (step_number, subrule_id|row_index) -> decision dict, recursively."""
    out: dict[tuple, dict] = {}

    def _walk(step_no, node):
        key = (step_no, node.get("subrule_id") or f"#{node.get('row_index')}")
        out[key] = node
        for c in node.get("children", []):
            _walk(step_no, c)

    for p in plan:
        for c in p["children"]:
            _walk(p["step_number"], c)
    return out


@pytest.mark.parametrize("name", ["Timely_Filing.yaml", "NpiMatch.yaml",
                                  "ProviderSelectionVerification.yaml"])
def test_html_door_matches_yaml_door(name):
    yaml_ir = _load_ir(name)
    yaml_plan = plan_ir(yaml_ir)

    # Build the HTML door's IR from rendered enriched steps via the a18 maker.
    from uhc_sop_ingestion.agents.a18_ir_synthesis import build_draft_ir
    draft = build_draft_ir({
        "metadata": {"title": yaml_ir.metadata.document_title},
        "enriched_steps": _render_enriched_steps(yaml_ir),
    })
    html_ir = SopIR.model_validate(draft)
    html_plan = plan_ir(html_ir)

    yaml_flat = _flatten(yaml_plan)
    html_flat = _flatten(html_plan)

    assert set(yaml_flat) == set(html_flat), (
        f"{name}: decision keys differ\n"
        f"  yaml-only: {sorted(set(yaml_flat) - set(html_flat))}\n"
        f"  html-only: {sorted(set(html_flat) - set(yaml_flat))}")

    mismatches = []
    for key, yd in yaml_flat.items():
        hd = html_flat[key]
        for f in _ROUTING_FIELDS:
            if yd.get(f) != hd.get(f):
                mismatches.append(f"{key}.{f}: yaml={yd.get(f)!r} html={hd.get(f)!r}")
        # applicable_when presence (the APPLICABLE_ONLY trigger) must match.
        if bool(yd.get("applicable_when")) != bool(hd.get("applicable_when")):
            mismatches.append(
                f"{key}.applicable_when: yaml={yd.get('applicable_when')!r} "
                f"html={hd.get('applicable_when')!r}")

    assert not mismatches, f"{name} routing parity mismatches:\n  " + "\n  ".join(mismatches)


def test_timely_filing_step1_routes_to_17():
    """Anchor assertion: the canonical Timely Filing 'Not Met -> step 17' route."""
    ir = _load_ir("Timely_Filing.yaml")
    plan = plan_ir(ir)
    step1 = next(p for p in plan if p["step_number"] == 1)
    leaf = step1["children"][0]
    assert leaf["goto_step"] == 17


def test_engine_routing_contract_present():
    """Every planned decision must expose the fields the engine cursor reads."""
    for name in ("Timely_Filing.yaml", "NpiMatch.yaml"):
        plan = plan_ir(_load_ir(name))
        for node in _flatten(plan).values():
            assert "goto_step" in node
            assert node["aggregation"] in {
                "FIRST_MATCH", "XOR_ONE", "APPLICABLE_ONLY",
                "ALWAYS_MET", "ANY", "LEAF"}
            assert isinstance(node["is_out_of_scope"], bool)
            assert isinstance(node["is_final"], bool)
