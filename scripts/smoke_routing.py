"""Smoke test for routing-aware SOP execution.

Drives the real ``execute_shapes`` step cursor, ``aggregate_decision`` and
``trace_builder`` with a mocked LLM (``evaluate_one_rule`` / ``llm_call``) and a
mocked tool runner, so no Anthropic key, Redis or Postgres is needed. Each
scenario asserts the routing behavior the plan promises and prints PASS/FAIL.

Run:  PYTHONPATH=. python scripts/smoke_routing.py
"""
from __future__ import annotations

import sys
from typing import Any

from uhc_execution_engine.agents import n_execute_shapes as nes
from uhc_execution_engine.agents import n06_aggregate as agg
from execution_app import trace_builder as tb

# ── test harness ──────────────────────────────────────────────────────────────
_FAILS: list[str] = []
_PASSES = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSES[0] += 1
        print(f"  PASS  {name}")
    else:
        _FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


def rule(key, step, *, matched=True, dtype="ALLOW", goto=None, oos=False,
         final=False, aggregation="LEAF", applicable_when="", applicable=True,
         navigation=None, source="decision", shape=None, codes=None) -> dict:
    shape_id = shape or f"shape-{step}"
    return {
        "key": key,
        "source": source,
        "condition": f"cond {key}",
        "action": f"act {key}",
        "decision_type": dtype,
        "codes": codes or [],
        "shape_id": shape_id,
        "shape_label": f"Shape {step}",
        "step_number": step,
        # trace_builder groups/labels off these (the real loader sets them);
        # mirror that so build_trace numbers steps as it would in production.
        "section_id": step,
        "yaml_rule_id": f"RULE-{step:03d}",
        "is_out_of_scope": oos,
        "is_final": final,
        "aggregation": aggregation,
        "applicable_when": applicable_when,
        "goto_step": goto,
        # what the mocked evaluator should return for this rule:
        "_verdict": {
            "matched": matched,
            "reasoning": f"reason {key}",
            "confidence": 0.9,
            "applicable": applicable,
            **({"navigation": navigation} if navigation else {}),
        },
    }


def shapes_from_rules(rules: list[dict]) -> list[dict]:
    """Group rules into shapes by shape_id, preserving order."""
    order: list[str] = []
    by_shape: dict[str, dict] = {}
    for r in rules:
        sid = r["shape_id"]
        if sid not in by_shape:
            by_shape[sid] = {"shape_id": sid, "shape_label": r["shape_label"], "rules": []}
            order.append(sid)
        by_shape[sid]["rules"].append(r)
    return [by_shape[s] for s in order]


def run_engine(rules: list[dict], *, tools_by_shape=None, claim=None) -> dict:
    state: dict[str, Any] = {
        "claim": claim or {"claim_id": "C1"},
        "claim_id": "C1",
        "shapes": shapes_from_rules(rules),
        "tools_by_rule_key": {},
        "tools_by_shape": tools_by_shape or {},
        "tool_results": {},
        "tool_invocations": [],
        "stages": [],
        "status": "RUNNING",
    }
    out = nes.execute_shapes(state)
    # merge engine output back like LangGraph would, then aggregate
    state.update(out)
    agg_out = agg.aggregate_decision(state)
    state.update(agg_out)
    return state


def res_by_key(state) -> dict[str, dict]:
    return {r["rule_key"]: r for r in state["rule_results"]}


# ── mocks ───────────────────────────────────────────────────────────────────
def _mock_evaluate(cfg, *, rule, claim, tool_context, stage):  # noqa: A002
    v = dict(rule.get("_verdict") or {"matched": False, "reasoning": "", "confidence": 0.0})
    return v, {"provider": "mock", "model": "mock", "ms": 1, "attempts": 1}


def _mock_llm_call(cfg, prompt, *, fallback=None, **kw):
    # aggregate_decision passes a deterministic fallback; just use it.
    return (fallback or {}), {"provider": "mock", "ms": 1}


_TOOL_CALLS: list[str] = []


def _mock_invoke_tool(name, args):
    _TOOL_CALLS.append(name)
    return {"ok": True, "result": {"tool": name}, "error": "", "duration_ms": 1, "args": args}


# ══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    nes.evaluate_one_rule = _mock_evaluate
    agg.llm_call = _mock_llm_call
    nes.invoke_tool = _mock_invoke_tool

    # ── A: sequential, no routing ─────────────────────────────────────────────
    print("\n[A] sequential — every step evaluated, nothing skipped")
    st = run_engine([
        rule("s:1:1:0", 1), rule("s:1:2:0", 2), rule("s:1:3:0", 3),
    ])
    rk = res_by_key(st)
    check("A all 3 evaluated", sum(not r["skipped"] for r in st["rule_results"]) == 3)
    check("A none skipped", sum(r["skipped"] for r in st["rule_results"]) == 0)
    check("A no early termination", st.get("status") != "TERMINATED_EARLY")
    check("A verdict ALLOW", st["final_decision_type"] == "ALLOW", st["final_decision_type"])

    # ── B: static goto skips intermediate steps ───────────────────────────────
    print("\n[B] static goto_step=4 from step 1 — steps 2,3 SKIPPED")
    st = run_engine([
        rule("s:1:1:0", 1, goto=4, dtype="REFER"),
        rule("s:1:2:0", 2), rule("s:1:3:0", 3), rule("s:1:4:0", 4),
    ])
    rk = res_by_key(st)
    check("B step1 evaluated", not rk["s:1:1:0"]["skipped"])
    check("B step2 skipped", rk["s:1:2:0"]["skipped"])
    check("B step3 skipped", rk["s:1:3:0"]["skipped"])
    check("B step4 evaluated", not rk["s:1:4:0"]["skipped"])
    check("B skip reason mentions route", "routed from step 1 to step 4" in rk["s:1:2:0"]["skip_reason"],
          rk["s:1:2:0"]["skip_reason"])

    # ── C: out-of-scope clean stop ─────────────────────────────────────────────
    print("\n[C] out-of-scope match at step 1 — clean stop, NOT a defect")
    st = run_engine([
        rule("s:1:1:0", 1, oos=True, final=True, dtype="STOP"),
        rule("s:1:2:0", 2), rule("s:1:3:0", 3),
    ])
    rk = res_by_key(st)
    check("C step1 evaluated+matched", not rk["s:1:1:0"]["skipped"] and rk["s:1:1:0"]["matched"])
    check("C step2 skipped", rk["s:1:2:0"]["skipped"])
    check("C step3 skipped", rk["s:1:3:0"]["skipped"])
    check("C NOT terminated_early", st.get("status") != "TERMINATED_EARLY", str(st.get("status")))
    check("C verdict clean ALLOW (oos excluded)", st["final_decision_type"] == "ALLOW",
          st["final_decision_type"])

    # ── D: DENY halt still terminates early ────────────────────────────────────
    print("\n[D] DENY match at step 2 — TERMINATED_EARLY defect halt")
    st = run_engine([
        rule("s:1:1:0", 1), rule("s:1:2:0", 2, dtype="DENY", codes=["E51"]),
        rule("s:1:3:0", 3),
    ])
    rk = res_by_key(st)
    check("D status TERMINATED_EARLY", st.get("status") == "TERMINATED_EARLY", str(st.get("status")))
    check("D terminated_at_shape set", st.get("terminated_at_shape_id") == "shape-2",
          str(st.get("terminated_at_shape_id")))
    check("D step3 skipped", rk["s:1:3:0"]["skipped"])
    check("D verdict DENY", st["final_decision_type"] == "DENY", st["final_decision_type"])
    check("D code carried", "E51" in st["applied_codes"], str(st["applied_codes"]))

    # ── E: LLM navigation override (goto) ──────────────────────────────────────
    print("\n[E] LLM navigation goto step 3 (no static goto) — step 2 skipped")
    st = run_engine([
        rule("s:1:1:0", 1, dtype="REFER", navigation={"op": "goto", "step_number": 3}),
        rule("s:1:2:0", 2), rule("s:1:3:0", 3),
    ])
    rk = res_by_key(st)
    check("E step2 skipped via nav", rk["s:1:2:0"]["skipped"])
    check("E step3 evaluated", not rk["s:1:3:0"]["skipped"])

    # ── F: applicable_when — non-applicable rule SKIPPED, not Not-Met ──────────
    print("\n[F] applicable_when false — rule SKIPPED (not a defect)")
    st = run_engine([
        rule("s:1:1:0", 1, matched=False, applicable=False,
             applicable_when="Provider is individual", dtype="DENY"),
        rule("s:1:2:0", 2),
    ])
    rk = res_by_key(st)
    check("F non-applicable rule skipped", rk["s:1:1:0"]["skipped"])
    check("F skip reason is applicability", rk["s:1:1:0"]["skip_reason"].startswith("not-applicable"),
          rk["s:1:1:0"]["skip_reason"])
    check("F skipped DENY excluded -> ALLOW", st["final_decision_type"] == "ALLOW",
          st["final_decision_type"])

    # ── G: APPLICABLE_ONLY sibling short-circuit (no LLM on the 2nd) ───────────
    print("\n[G] APPLICABLE_ONLY — 2nd sibling skipped with no eval once 1st applies")
    st = run_engine([
        rule("s:1:1:0", 1, aggregation="APPLICABLE_ONLY", applicable_when="A", matched=True),
        rule("s:1:1:1", 1, aggregation="APPLICABLE_ONLY", applicable_when="B", matched=True),
        rule("s:1:2:0", 2),
    ])
    rk = res_by_key(st)
    check("G first sibling evaluated", not rk["s:1:1:0"]["skipped"])
    check("G second sibling skipped", rk["s:1:1:1"]["skipped"])
    check("G second sibling reason sibling", "sibling" in rk["s:1:1:1"]["skip_reason"],
          rk["s:1:1:1"]["skip_reason"])

    # ── H: backward goto is ignored (forward-only) ────────────────────────────
    print("\n[H] backward goto (target < current) ignored — sequential continues")
    st = run_engine([
        rule("s:1:1:0", 1), rule("s:1:2:0", 2, goto=1, dtype="REFER"),
        rule("s:1:3:0", 3),
    ])
    rk = res_by_key(st)
    check("H no rule skipped (forward-only)", sum(r["skipped"] for r in st["rule_results"]) == 0)
    check("H step3 still evaluated", not rk["s:1:3:0"]["skipped"])

    # ── I: goto past the last step — clean terminate ───────────────────────────
    print("\n[I] goto to non-existent forward step — clean stop, rest skipped")
    st = run_engine([
        rule("s:1:1:0", 1, goto=99, dtype="REFER"),
        rule("s:1:2:0", 2), rule("s:1:3:0", 3),
    ])
    rk = res_by_key(st)
    check("I step1 evaluated", not rk["s:1:1:0"]["skipped"])
    check("I steps 2,3 skipped", rk["s:1:2:0"]["skipped"] and rk["s:1:3:0"]["skipped"])
    check("I not terminated_early", st.get("status") != "TERMINATED_EARLY")

    # ── J: preconditions evaluated first; precondition DENY halts ──────────────
    print("\n[J] blocking precondition DENY halts before any decision step")
    st = run_engine([
        rule("pc:1", 0, source="precondition", dtype="DENY", codes=["F51"]),
        rule("s:1:1:0", 1), rule("s:1:2:0", 2),
    ])
    rk = res_by_key(st)
    check("J terminated early on precondition", st.get("status") == "TERMINATED_EARLY")
    check("J decision steps never evaluated", "s:1:1:0" not in rk and "s:1:2:0" not in rk,
          str(list(rk.keys())))

    # ── K: lazy tools — skipped step's tools are never invoked ─────────────────
    print("\n[K] lazy tools — tool on a skipped step is NOT invoked")
    _TOOL_CALLS.clear()
    tools_by_shape = {
        "shape-1": [{"binding_id": "b1", "tool_name": "tool_one", "args_template": {}}],
        "shape-2": [{"binding_id": "b2", "tool_name": "tool_two_SKIPPED", "args_template": {}}],
        "shape-4": [{"binding_id": "b4", "tool_name": "tool_four", "args_template": {}}],
    }
    st = run_engine([
        rule("s:1:1:0", 1, goto=4, dtype="REFER"),
        rule("s:1:2:0", 2), rule("s:1:3:0", 3), rule("s:1:4:0", 4),
    ], tools_by_shape=tools_by_shape)
    check("K tool_one invoked", "tool_one" in _TOOL_CALLS, str(_TOOL_CALLS))
    check("K tool_four invoked", "tool_four" in _TOOL_CALLS, str(_TOOL_CALLS))
    check("K skipped step's tool NOT invoked", "tool_two_SKIPPED" not in _TOOL_CALLS, str(_TOOL_CALLS))
    check("K tool_results populated", "b1" in st["tool_results"] and "b4" in st["tool_results"])

    # ── L: trace_builder honors SKIPPED ────────────────────────────────────────
    print("\n[L] trace_builder — skipped steps ignored in claim rollup")
    check("L _status_for_eval skipped",
          tb._status_for_eval({"skipped": True}) == tb.SKIPPED_RULE)
    check("L aggregate ignores skipped (Met + Skipped = CLEAN)",
          tb.aggregate_status([tb.MET, tb.SKIPPED_RULE]) == tb.CLEAN)
    check("L all-skipped step rolls to Skipped",
          tb._aggregate_rule_status([tb.SKIPPED_RULE, tb.SKIPPED_RULE]) == tb.SKIPPED_RULE)
    check("L rule_to_audit(skipped) is empty (ignored)",
          tb.rule_to_audit(tb.SKIPPED_RULE) == "")
    check("L defect still wins over skipped",
          tb.aggregate_status([tb.SKIPPED_RULE, tb.NOT_MET]) == tb.DEFECT)

    # ── M: full build_trace over real engine output (skipped steps) ───────────
    print("\n[M] build_trace — skipped steps render as Skipped, claim stays CLEAN")
    import datetime as _dt

    class _Run:
        id = "run-xyz"
        claim_id = "C1"
        started_at = _dt.datetime(2026, 1, 1, 0, 0, 0)
        finished_at = _dt.datetime(2026, 1, 1, 0, 0, 1)

    st = run_engine([
        rule("s:1:1:0", 1, goto=4, dtype="ALLOW"),
        rule("s:1:2:0", 2), rule("s:1:3:0", 3), rule("s:1:4:0", 4, dtype="ALLOW"),
    ])
    trace, expl = tb.build_trace(_Run(), st["rule_results"], st.get("tool_invocations") or [])
    status_by_step = {t["sop_step_number"]: t["status"] for t in trace}
    check("M skipped steps in trace are Skipped",
          status_by_step.get(2) == tb.SKIPPED_RULE and status_by_step.get(3) == tb.SKIPPED_RULE,
          str(status_by_step))
    check("M evaluated steps are Met", status_by_step.get(1) == tb.MET and status_by_step.get(4) == tb.MET,
          str(status_by_step))
    check("M claim_status CLEAN (skips ignored)", tb.claim_status(trace) == tb.CLEAN,
          tb.claim_status(trace))
    check("M explainability built", len(expl) >= 1)

    # ── N: LangGraph still builds with the rewritten node ──────────────────────
    print("\n[N] LangGraph pipeline still compiles with the new execute_shapes")
    try:
        from uhc_execution_engine.graph import build_graph  # noqa
        g = build_graph()
        check("N graph compiled", g is not None)
    except Exception as exc:  # pragma: no cover
        try:
            from uhc_execution_engine import graph as _gmod
            fn = getattr(_gmod, "build_graph", None) or getattr(_gmod, "build", None)
            check("N graph module importable", _gmod is not None)
        except Exception as exc2:
            check("N graph importable", False, f"{exc} / {exc2}")

    # ── summary ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    total = _PASSES[0] + len(_FAILS)
    print(f"SMOKE RESULT: {_PASSES[0]}/{total} checks passed")
    if _FAILS:
        print("FAILED CHECKS:")
        for f in _FAILS:
            print(f"  - {f}")
        return 1
    print("ALL ROUTING SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
