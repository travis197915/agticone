"""execute_shapes must run each SOP's step cursor in its own namespace.

A claim-audit workflow can chain several SOPs (Initial Verification → Member
Eligibility → … → Coverage). Every SOP numbers its steps 1..N independently and
routes ("skip to step 17") within that local namespace. These tests pin the
two invariants that make a multi-SOP workflow correct:

* **Namespacing** — a ``goto`` inside SOP-A only skips SOP-A's steps; SOP-B
  still runs every step even when it reuses the same step numbers.
* **Single-SOP parity** — with one SOP the cursor behaves exactly as before
  (intra-SOP ``goto`` skips the intermediate steps).

The LLM evaluator + event bus are stubbed so the tests are deterministic and
need no DB or network.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from uhc_execution_engine.agents import n_execute_shapes


def _rule(sop_id: int, step_number: int, row_index: int, **over) -> dict:
    """Build one decision rule dict in the shape rule_loader emits."""
    base = {
        "key": f"step:{sop_id}:{step_number}:{row_index}",
        "sop_id": sop_id,
        "source": "decision",
        "step_number": step_number,
        "shape_id": f"shape-{sop_id}-{step_number}",
        "shape_label": f"SOP{sop_id} Step {step_number}",
        "decision_type": "REFER",
        "is_out_of_scope": False,
        "is_final": False,
        "aggregation": "LEAF",
        "applicable_when": "",
        "goto_step": None,
        "codes": [],
        "condition": "",
        "action": "",
    }
    base.update(over)
    return base


def _shapes_from_rules(rules: list[dict]) -> list[dict]:
    """Group rules into one shape per (sop_id, step_number), in list order."""
    shapes: list[dict] = []
    index: dict[tuple, dict] = {}
    for r in rules:
        k = (r["sop_id"], r["step_number"])
        grp = index.get(k)
        if grp is None:
            grp = {"shape_id": r["shape_id"], "shape_label": r["shape_label"],
                   "rules": []}
            index[k] = grp
            shapes.append(grp)
        grp["rules"].append(r)
    return shapes


class _FakeEval:
    """Deterministic ``evaluate_one_rule`` keyed by rule_key → verdict dict."""

    def __init__(self, verdicts: dict[str, dict]):
        self.verdicts = verdicts
        self.calls: list[str] = []

    def __call__(self, cfg, *, rule, claim, tool_context, stage):
        self.calls.append(rule["key"])
        v = dict(self.verdicts.get(rule["key"]) or {})
        v.setdefault("matched", False)
        v.setdefault("applicable", True)
        v.setdefault("confidence", 1.0)
        v.setdefault("reasoning", "stub")
        return v, {"provider": "stub", "ms": 0}


def _run(rules: list[dict], verdicts: dict[str, dict]) -> dict:
    state = {
        "status": "RUNNING",
        "claim": {"claim_id": "C1"},
        "claim_id": "C1",
        "shapes": _shapes_from_rules(rules),
        "tools_by_rule_key": {},
        "tools_by_shape": {},
        "tool_results": {},
        "tool_invocations": [],
        "stages": [],
    }
    fake = _FakeEval(verdicts)
    with mock.patch.object(n_execute_shapes, "evaluate_one_rule", fake), \
         mock.patch.object(n_execute_shapes, "publish_event", lambda *a, **k: None), \
         mock.patch.object(n_execute_shapes, "get_config",
                           lambda: SimpleNamespace(lazy_tools=False)):
        out = n_execute_shapes.execute_shapes(state)
    out["_eval_calls"] = fake.calls
    return out


def _by_key(results: list[dict]) -> dict[str, dict]:
    return {r["rule_key"]: r for r in results}


class MultiSopNamespacingTests(SimpleTestCase):
    def test_goto_is_scoped_to_its_own_sop(self):
        # SOP 100: step 1 routes (goto) to step 3 → step 2 of SOP 100 skipped.
        # SOP 200: steps 1 & 2 must BOTH run — proving SOP 100's "skip step 2"
        #          does not leak into SOP 200's identically-numbered step 2.
        rules = [
            _rule(100, 1, 0, goto_step=3),
            _rule(100, 2, 0),
            _rule(100, 3, 0),
            _rule(200, 1, 0),
            _rule(200, 2, 0),
        ]
        verdicts = {
            "step:100:1:0": {"matched": True,
                             "navigation": {"op": "goto", "step_number": 3}},
        }
        out = _run(rules, verdicts)
        res = _by_key(out["rule_results"])

        # SOP 100: step 2 skipped by the goto, steps 1 & 3 evaluated.
        self.assertFalse(res["step:100:1:0"]["skipped"])
        self.assertTrue(res["step:100:2:0"]["skipped"])
        self.assertFalse(res["step:100:3:0"]["skipped"])

        # SOP 200: nothing skipped — its own cursor ran both steps.
        self.assertFalse(res["step:200:1:0"]["skipped"])
        self.assertFalse(res["step:200:2:0"]["skipped"])

        # The skipped step never reached the evaluator.
        self.assertNotIn("step:100:2:0", out["_eval_calls"])
        self.assertIn("step:200:2:0", out["_eval_calls"])
        self.assertNotIn("status", out)  # no TERMINATED_EARLY

    def test_deny_halts_whole_claim_and_skips_later_sops(self):
        rules = [
            _rule(100, 1, 0, decision_type="DENY"),
            _rule(100, 2, 0),
            _rule(200, 1, 0),
        ]
        verdicts = {"step:100:1:0": {"matched": True}}
        out = _run(rules, verdicts)
        res = _by_key(out["rule_results"])

        self.assertEqual(out.get("status"), "TERMINATED_EARLY")
        self.assertEqual(out.get("terminated_at_shape_id"), "shape-100-1")
        # Remaining step of SOP 100 and all of SOP 200 are skipped, unevaluated.
        self.assertTrue(res["step:100:2:0"]["skipped"])
        self.assertTrue(res["step:200:1:0"]["skipped"])
        self.assertNotIn("step:200:1:0", out["_eval_calls"])

    def test_out_of_scope_ends_only_its_sop(self):
        # SOP 100 step 1 is out-of-scope (clean stop) → SOP 100 step 2 skipped,
        # but SOP 200 still runs fully.
        rules = [
            _rule(100, 1, 0, is_out_of_scope=True, is_final=True,
                  decision_type="STOP"),
            _rule(100, 2, 0),
            _rule(200, 1, 0),
        ]
        verdicts = {"step:100:1:0": {"matched": True}}
        out = _run(rules, verdicts)
        res = _by_key(out["rule_results"])

        self.assertNotIn("status", out)  # clean stop, not a defect halt
        self.assertTrue(res["step:100:2:0"]["skipped"])
        self.assertFalse(res["step:200:1:0"]["skipped"])
        self.assertIn("step:200:1:0", out["_eval_calls"])


class SingleSopParityTests(SimpleTestCase):
    def test_single_sop_goto_still_skips_intermediate_steps(self):
        rules = [
            _rule(1, 1, 0, goto_step=3),
            _rule(1, 2, 0),
            _rule(1, 3, 0),
        ]
        verdicts = {
            "step:1:1:0": {"matched": True,
                           "navigation": {"op": "goto", "step_number": 3}},
        }
        out = _run(rules, verdicts)
        res = _by_key(out["rule_results"])
        self.assertFalse(res["step:1:1:0"]["skipped"])
        self.assertTrue(res["step:1:2:0"]["skipped"])
        self.assertFalse(res["step:1:3:0"]["skipped"])
        self.assertNotIn("step:1:2:0", out["_eval_calls"])
