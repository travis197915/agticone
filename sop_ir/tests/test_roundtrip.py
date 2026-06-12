"""Baseline contract tests for the canonical SOP IR.

These run as *pure* pytest (no Django DB) so they can gate the IR contract in
isolation:

  * every hand-authored SOP-rule YAML parses into a :class:`SopIR`
  * ``validate_ir`` runs on each and we report (don't hard-fail) which files
    violate routing invariants today — a regression baseline for the HTML/PDF
    parity work
  * the IR enums never drift from ``AuditDecision`` model choices

Run::

    pytest sop_ir/tests/test_roundtrip.py -v -s
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from sop_ir.schema import Aggregation, DecisionType, SopIR
from sop_ir.validate import validate_ir

REPO_ROOT = Path(__file__).resolve().parents[2]
YAML_DIR = REPO_ROOT / "yaml"


def _all_yaml_files():
    return sorted(YAML_DIR.glob("*.yaml"))


def _load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _is_sop_rule_doc(doc) -> bool:
    return isinstance(doc, dict) and isinstance(doc.get("sop_rules"), list) and bool(doc["sop_rules"])


def test_yaml_dir_present():
    files = _all_yaml_files()
    assert files, f"no yaml files found under {YAML_DIR}"


@pytest.mark.parametrize("path", _all_yaml_files(), ids=lambda p: p.name)
def test_sop_rule_yaml_parses_into_ir(path: Path):
    """Every YAML that has sop_rules must parse losslessly into a SopIR.

    Grounding / tool-map YAMLs (no sop_rules) are skipped — they are not SOPs.
    """
    doc = _load(path)
    if not _is_sop_rule_doc(doc):
        pytest.skip(f"{path.name} is not a SOP-rule YAML (no sop_rules)")

    ir = SopIR.from_yaml_doc(doc)
    assert ir.rules, f"{path.name} parsed to an IR with no rules"
    # round-trip through JSON to prove the model is fully serialisable (the
    # exact payload persist_ir will hand to Mongo).
    dumped = ir.model_dump(mode="json")
    again = SopIR.model_validate(dumped)
    assert len(again.rules) == len(ir.rules)


def test_validation_baseline_report(capsys):
    """Report (not assert) the validation status of every SOP-rule YAML.

    This is the regression baseline: as the HTML/PDF door is brought to parity
    we want to see these violations trend to zero, but pre-existing authoring
    quirks must not block the build.
    """
    summary = []
    for path in _all_yaml_files():
        doc = _load(path)
        if not _is_sop_rule_doc(doc):
            continue
        ir = SopIR.from_yaml_doc(doc)
        ok, errors = validate_ir(ir)
        hard = [e for e in errors if e.startswith("ERROR:")]
        summary.append((path.name, ok, len(hard), len(errors) - len(hard)))
        with capsys.disabled():
            status = "OK  " if ok else "FAIL"
            print(f"  [{status}] {path.name:<34} "
                  f"{len(hard)} errors / {len(errors) - len(hard)} warnings")
            for e in errors:
                print(f"           {e}")

    assert summary, "expected at least one SOP-rule YAML to validate"


def test_ir_enums_mirror_model_choices():
    """The IR enums must never drift from AuditDecision.*_CHOICES."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")
    try:
        import django
        django.setup()
        from sop_ingestion.models import AuditDecision
    except Exception as exc:  # pragma: no cover - env without Django configured
        pytest.skip(f"Django not configured for enum-parity check: {exc}")

    model_decisions = {c[0] for c in AuditDecision.DECISION_CHOICES}
    ir_decisions = {d.value for d in DecisionType}
    assert ir_decisions == model_decisions, (
        f"DecisionType drift: IR={ir_decisions} model={model_decisions}")

    model_aggs = {c[0] for c in AuditDecision.AGGREGATION_CHOICES}
    ir_aggs = {a.value for a in Aggregation}
    assert ir_aggs == model_aggs, (
        f"Aggregation drift: IR={ir_aggs} model={model_aggs}")
