"""
Cross-Prevalence Billing tool — slim repo-local port
(``check_cross_prevalence_billing``).

When ``AGENT_TOOLS_SQL_BACKEND=memory`` (the default), the lookup runs
against an in-memory table seeded from a tiny default rule set. To exercise
a richer set, callers can drop fixture rows via
:func:`agent_tools.tools.cross_prevalence_billing_tool._seed_default_rules`.
"""
from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from . import _sql_memory
from ._logging import get_logger
from .schemas.cross_prevalence import CrossPrevalenceBillingInput

LOGGER = get_logger("cross_prevalence")
TABLE = "cross_prevalence_billing_codes"

_SEEDED = False


def _seed_default_rules() -> None:
    """Idempotently seed a tiny default set of rules into the memory backend."""
    global _SEEDED
    if _SEEDED and _sql_memory.select(TABLE):
        return
    seeds = [
        {"cpt_pay": "99213", "cpt_deny": "99214", "modifier": "Allowed with 25"},
        {"cpt_pay": "99214", "cpt_deny": "99215", "modifier": "Not allowed"},
        {"cpt_pay": "00104", "cpt_deny": "00103", "modifier": "Allowed with 59"},
    ]
    for row in seeds:
        _sql_memory.upsert(TABLE, ["cpt_pay", "cpt_deny"], row)
    _SEEDED = True


def _split_cpt(value: str) -> tuple[str, str | None]:
    """Compact numeric inputs: > 5 digits ⇒ CPT (first 5) + required modifier (last 2)."""
    code = (value or "").strip().upper()
    if len(code) > 5 and code.isdigit():
        return code[:5], code[-2:]
    return code, None


def _check(**kwargs: Any) -> dict[str, Any]:
    if not _sql_memory.using_memory():
        return {
            "ok": False, "found": False,
            "message": "Non-memory SQL backend not supported in this build",
            "matches": [], "bootstrapped": False,
        }
    _seed_default_rules()

    a, mod_a = _split_cpt(kwargs.get("cpt_code_a", ""))
    b, mod_b = _split_cpt(kwargs.get("cpt_code_b", ""))
    required = [m for m in (mod_a, mod_b) if m]

    matches = [
        r for r in _sql_memory.select(TABLE)
        if (r["cpt_pay"] == a and r["cpt_deny"] == b) or
           (r["cpt_pay"] == b and r["cpt_deny"] == a)
    ]
    if not matches:
        return {
            "ok": True, "found": False,
            "cpt_pay": None, "cpt_deny": None, "modifier": None,
            "message": f"No cross-prevalence rule for {a} / {b}",
            "matches": [],
            "required_modifiers": required,
            "modifiers_satisfied": None,
            "missing_required_modifiers": [],
            "bootstrapped": False,
        }
    top = matches[0]
    modifier_text = top.get("modifier", "")
    satisfied = None
    missing: list[str] = []
    if required:
        present = [m for m in required if m in modifier_text]
        satisfied = len(present) == len(required)
        missing = [m for m in required if m not in present]
    return {
        "ok": True, "found": True,
        "cpt_pay": top["cpt_pay"],
        "cpt_deny": top["cpt_deny"],
        "modifier": modifier_text,
        "message": modifier_text,
        "matches": matches,
        "required_modifiers": required,
        "modifiers_satisfied": satisfied,
        "missing_required_modifiers": missing,
        "bootstrapped": False,
    }


def build_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="check_cross_prevalence_billing",
        description=(
            "Look up cross-prevalence billing restrictions for a CPT pair "
            "(with optional embedded modifiers). Backed by the in-memory "
            "SQL shim in this build."
        ),
        func=lambda **kwargs: _check(**kwargs),
        args_schema=CrossPrevalenceBillingInput,
    )
