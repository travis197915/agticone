"""JSONParserAgent — parses response body as JSON when possible.

Pure best-effort: if the body isn't valid JSON we just flag is_json=False and
leave the raw text in place; we never fail the call for that reason alone.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import AgentState
    from ..config import AgentConfig


def json_parser(state: "AgentState", cfg: "AgentConfig") -> dict:
    if state.get("error"):
        return {}
    if state.get("cache_hit"):
        # already parsed when we read from cache
        return {"stages": [{"agent": "JSONParserAgent", "status": "OK", "msg": "skipped (cache hit)"}]}

    text = state.get("response_text") or ""
    if not text.strip():
        return {
            "is_json": False,
            "json": None,
            "stages": [{"agent": "JSONParserAgent", "status": "OK", "msg": "empty body"}],
        }

    try:
        parsed = json.loads(text)
        return {
            "is_json": True,
            "json": parsed,
            "stages": [{
                "agent": "JSONParserAgent",
                "status": "OK",
                "msg": f"parsed type={type(parsed).__name__}",
            }],
        }
    except json.JSONDecodeError as e:
        return {
            "is_json": False,
            "json": None,
            "stages": [{
                "agent": "JSONParserAgent",
                "status": "OK",
                "msg": f"non-json body ({e.msg})",
            }],
        }
