"""DB-free smoke test of the SOP YAML importer's parsing/planning logic.

Exercises ``import_sop_yaml`` planning (``_plan_rule`` and helpers) on the real
YAML files so we know goto / out-of-scope / applicable_when / aggregation_rule
are extracted and propagated into the decision tree the loader later reads.
No database is touched (we only call the pure planning methods).

Run:  DJANGO_SETTINGS_MODULE=sop_backend.settings PYTHONPATH=. python scripts/smoke_import.py
"""
from __future__ import annotations

import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")
django.setup()

import yaml  # noqa: E402

from sop_ingestion.management.commands.import_sop_yaml import (  # noqa: E402
    Command, _extract_goto, _is_out_of_scope, _map_aggregation_rule,
)

_FAILS: list[str] = []
_PASSES = [0]


def check(name, cond, detail=""):
    if cond:
        _PASSES[0] += 1
        print(f"  PASS  {name}")
    else:
        _FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


def plan_yaml(path):
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    rules = doc.get("sop_rules") or []
    cmd = Command()
    return [cmd._plan_rule(r, i) for i, r in enumerate(rules)]


def walk(node):
    yield node
    for c in node.get("children", []):
        yield from walk(c)


def main():
    print("\n[unit] helper parsing")
    check("goto 'skip to Step 4'", _extract_goto("Skip to Step 4 for that line item") == 4)
    check("goto 'proceed to step 8 directly'", _extract_goto("proceed to step 8 directly") == 8)
    check("goto none", _extract_goto("deny the line item") is None)
    check("oos detect", _is_out_of_scope("Stop further auditing as this is out of scope"))
    check("oos negative", not _is_out_of_scope("Deny the claim line"))
    check("agg applicable_only", _map_aggregation_rule("applicable_only") == "APPLICABLE_ONLY")
    check("agg any_clean", _map_aggregation_rule("any_clean") == "ANY")
    check("agg none", _map_aggregation_rule("") is None)

    # ── NpiMatch: applicable_when + aggregation_rule should propagate ──────────
    print("\n[NpiMatch.yaml] applicable_when + aggregation propagation")
    plan = plan_yaml("yaml/NpiMatch.yaml")
    all_nodes = [n for p in plan for n in walk(p)]
    aw_nodes = [n for n in all_nodes if n.get("applicable_when")]
    check("NpiMatch has applicable_when rows", len(aw_nodes) > 0, f"found {len(aw_nodes)}")
    # any rule carrying applicable_when must have its sibling group forced
    # APPLICABLE_ONLY (so the engine evaluates only the applicable branch)
    forced = [n for n in all_nodes if n.get("aggregation") == "APPLICABLE_ONLY"]
    check("NpiMatch forces APPLICABLE_ONLY", len(forced) > 0, f"found {len(forced)}")

    # ── ProviderOptout: aggregation_rule: any_clean -> ANY propagation ─────────
    print("\n[ProviderOptoutAuditAgent.yaml] aggregation_rule any_clean -> ANY")
    plan = plan_yaml("yaml/ProviderOptoutAuditAgent.yaml")
    all_nodes = [n for p in plan for n in walk(p)]
    any_nodes = [n for n in all_nodes if n.get("aggregation") == "ANY"]
    check("ProviderOptout propagates ANY aggregation", len(any_nodes) > 0,
          f"found {len(any_nodes)}")

    # ── DuplicateVerification: goto + out-of-scope captured ────────────────────
    print("\n[DuplicateVerification.yaml] goto + out-of-scope captured")
    plan = plan_yaml("yaml/DuplicateVerification.yaml")
    all_nodes = [n for p in plan for n in walk(p)]
    gotos = [n["goto_step"] for n in all_nodes if n.get("goto_step")]
    oos = [n for n in all_nodes if n.get("is_out_of_scope")]
    check("DuplicateVerification has goto rows", len(gotos) > 0, f"gotos={gotos}")
    check("DuplicateVerification has out-of-scope rows", len(oos) > 0, f"found {len(oos)}")

    # ── all 13 YAMLs parse + plan without error ────────────────────────────────
    print("\n[all] every shipped SOP YAML plans without error")
    import glob
    n_ok = 0
    for path in sorted(glob.glob("yaml/*.yaml")):
        try:
            doc = yaml.safe_load(open(path, encoding="utf-8"))
            if not isinstance(doc, dict) or not doc.get("sop_rules"):
                continue  # ontology/mapping/config helpers, not rule SOPs
            plan_yaml(path)
            n_ok += 1
        except Exception as exc:  # pragma: no cover
            check(f"plan {os.path.basename(path)}", False, str(exc))
    check("rule SOPs planned cleanly", n_ok >= 5, f"planned {n_ok}")

    print("\n" + "=" * 70)
    total = _PASSES[0] + len(_FAILS)
    print(f"IMPORT SMOKE RESULT: {_PASSES[0]}/{total} checks passed")
    if _FAILS:
        print("FAILED:")
        for f in _FAILS:
            print("  -", f)
        return 1
    print("ALL IMPORT SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
