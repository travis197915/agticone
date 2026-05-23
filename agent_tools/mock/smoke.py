"""
Mock smoke-test driver — one canonical request per LangChain tool, with the
matching response printed.

Run it against a live Django server (``python manage.py runserver``); the
script mints its own JWT from ``JWT_SECRET`` so you don't need to copy a
token in from anywhere.

    cd uhc-backend-v2
    python -m agent_tools.mock.smoke                     # all 18 tools
    python -m agent_tools.mock.smoke facets_get_summary  # just one
    python -m agent_tools.mock.smoke --base http://localhost:8000

The request bodies below mirror the tool's Pydantic ``args_schema``; the
matching response is whatever the LangGraph runtime returns after the
tool calls the in-process mock upstream (``/api/mocks/...``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import jwt
import requests
from dotenv import load_dotenv


# Each row = (tool_name, request_args_dict). All requests are POST'd as
# ``{"args": <dict>}`` to /api/agent-tools/{tool_name}/invoke.
TOOLS: list[tuple[str, dict[str, Any]]] = [
    # ── DOC360 ──────────────────────────────────────────────────────────
    ("doc360_read_claim_by_fln_dcc", {
        "fln_dcc": "1234567890",
    }),
    # ── Facets ──────────────────────────────────────────────────────────
    ("facets_get_summary",            {"claim_number": "25XG44660400"}),
    ("facets_get_cob",                {"claim_number": "25XG44660400"}),
    ("facets_get_line_details",       {"claim_number": "25XG44660400"}),
    ("facets_get_member_eligibility", {"claim_number": "25XG44660400"}),
    ("facets_get_provider_details",   {"claim_number_for_reference": "25XG44660400"}),
    ("facets_get_duplicate_claim",    {"claim_number": "25XG44660400"}),
    # ── Facet Extension Portal ──────────────────────────────────────────
    ("facet_extension_portal_provider",   {"provider_id": "FAC000022500"}),
    ("facet_extension_portal_programme",  {"program_detailed_id": "276728"}),
    ("facet_ext_portal_group_model",      {"claim_number": "25XG44660400"}),
    # ── CBD coverage ────────────────────────────────────────────────────
    ("check_medicare_coverage", {
        "cpt_codes":  ["99213", "99214"],
        "group_name": "Standard Medicare",
        "plan_name":  "Standard Medicare",
    }),
    # ── Diagnosis ───────────────────────────────────────────────────────
    ("check_diagnosis_coverage", {"diagnosis_code": "E11.9"}),
    # ── LINX ────────────────────────────────────────────────────────────
    ("linx_claim_search", {
        "subscriber_id": "SUB-12345",
        "first_name":    "JANE",
        "last_name":     "DOE",
        "dob":           "01/24/1980",
        "start_date":    "01/01/2025",
        "end_date":      "01/31/2025",
    }),
    # ── CMS Medicare opt-out ────────────────────────────────────────────
    ("medicare_optout_checker", {"last_name": "DOE", "state": "MA"}),
    # ── Cross-prevalence billing (in-memory SQL backend) ────────────────
    ("check_cross_prevalence_billing", {
        "cpt_code_a": "99213",
        "cpt_code_b": "99214",
    }),
    # ── save_sop_step (in-memory SQL backend) ───────────────────────────
    ("save_sop_step", {
        "execution_id":      "exec-001",
        "claim_id":          "25XG44660400",
        "agent_name":        "smoke-test",
        "sop_name":          "OBH Facets Timely Filing",
        "sop_step_number":   1,
        "sop_step_name":     "Compute filing window",
        "sop_action":        "Compute DOS → received-date delta",
        "step_exec_status":  "SUCCESS",
        "status":            "ok",
        "result_summary":    "within 90 days",
        "rationale":         "DOS = 2025-01-24, received = 2025-02-03",
        "evidence_refs":     ["facets_get_summary", "facets_get_line_details"],
        "tools_used":        ["facets_get_summary", "facets_get_line_details"],
        "tools_succeeded":   ["facets_get_summary", "facets_get_line_details"],
    }),
    # ── LLM (mock mode) ─────────────────────────────────────────────────
    ("llm_parse_claim_with_ontology", {
        "claim_data": {
            "content": (
                "MOCK PRINT IMAGE\n"
                "TOTAL CHARGE $150.00\n"
                "21 DIAGNOSIS 1 F33.2 2 E11.8\n"
            ),
        },
    }),
    # ── Electronic (regex) parser ───────────────────────────────────────
    ("claim_parse_flat_template_with_confidence", {
        "claim_data": {
            "content": (
                "Box 21: 1 I10 2 E11.9\n"
                "TOTAL CHARGE $250.00\n"
            ),
        },
    }),
]


# ── helpers ────────────────────────────────────────────────────────────────


def mint_jwt() -> str:
    """Sign a short-lived JWT with the same secret the Django auth backend uses."""
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        sys.exit(
            "JWT_SECRET not in env. `source` your .env or export JWT_SECRET first."
        )
    payload = {
        "sub":   "smoke-test",
        "email": "smoke@example.com",
        "role":  "ADMIN",
        "iat":   int(time.time()),
        "exp":   int(time.time()) + 3600,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def invoke(base: str, token: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    url = f"{base.rstrip('/')}/api/agent-tools/{name}/invoke"
    resp = requests.post(
        url,
        json={"args": args},
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        timeout=60,
    )
    body: Any
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    return {"http_status": resp.status_code, "body": body}


def truncate(value: Any, *, max_chars: int = 1200) -> Any:
    text = json.dumps(value, indent=2, default=str)
    if len(text) <= max_chars:
        return value
    return {"_truncated": True, "preview": text[:max_chars] + "...[truncated]"}


# ── entry point ────────────────────────────────────────────────────────────


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base", default=os.environ.get("AGENT_TOOLS_BASE", "http://localhost:8000"),
        help="Django origin (default: %(default)s)",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Print untruncated response bodies (default: trim to 1200 chars).",
    )
    parser.add_argument(
        "tools", nargs="*",
        help="Optional tool names to run (default: all 18).",
    )
    args = parser.parse_args()

    token = mint_jwt()
    selected = set(args.tools)
    rows = [r for r in TOOLS if (not selected or r[0] in selected)]
    if selected:
        unknown = selected - {r[0] for r in TOOLS}
        if unknown:
            print(f"Unknown tools: {sorted(unknown)}", file=sys.stderr)
            return 2

    print(f"Calling {len(rows)} tool(s) at {args.base}\n")
    failures = 0
    for name, request in rows:
        print(f"── {name} {'─' * (74 - len(name))}")
        print("REQUEST:")
        print(json.dumps({"args": request}, indent=2))
        try:
            out = invoke(args.base, token, name, request)
        except requests.RequestException as exc:
            print(f"NETWORK ERROR: {exc}\n")
            failures += 1
            continue
        body = out["body"] if args.full else truncate(out["body"])
        print(f"RESPONSE (HTTP {out['http_status']}):")
        print(json.dumps(body, indent=2, default=str))
        print()
        if out["http_status"] != 200 or not (out["body"] or {}).get("ok", False):
            failures += 1

    print(f"Done. {len(rows) - failures}/{len(rows)} tools returned ok=true.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
