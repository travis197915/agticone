"""LangGraph wiring for the API agent.

Linear flow:
    validate_url → resolve_auth → api_caller → json_parser → response_logger
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents.a01_validate_url import url_validator
from .agents.a02_resolve_auth import auth_resolver
from .agents.a03_api_caller   import api_caller
from .agents.a04_parse_json   import json_parser
from .agents.a05_log_response import response_logger
from .config import AgentConfig


# ── Concrete LangGraph state (with reducer for stages list) ───────────────────
class _GraphState(TypedDict, total=False):
    url:        str
    method:     str
    body:       Any
    query:      dict
    headers:    dict
    auth_input: dict
    name:       str
    save_auth:  bool
    use_cache:  bool

    call_id:        str
    endpoint_id:    str
    resolved_auth:  dict
    request_headers: dict
    request_query:   dict

    status_code:    int
    duration_ms:    int
    response_bytes: int
    response_text:  str
    is_json:        bool
    json:           Any
    success:        bool
    error:          str
    cache_hit:      bool

    stages: Annotated[list, operator.add]   # accumulator


def _node(fn, cfg: AgentConfig):
    """Bind cfg into a LangGraph node fn(state) → dict."""
    def inner(state):
        return fn(state, cfg)
    inner.__name__ = fn.__name__
    return inner


def build_graph(cfg: AgentConfig):
    g = StateGraph(_GraphState)

    g.add_node("validate_url",  _node(url_validator,   cfg))
    g.add_node("resolve_auth",  _node(auth_resolver,   cfg))
    g.add_node("api_caller",    _node(api_caller,      cfg))
    g.add_node("json_parser",   _node(json_parser,     cfg))
    g.add_node("response_logger", _node(response_logger, cfg))

    g.add_edge(START,          "validate_url")
    g.add_edge("validate_url", "resolve_auth")
    g.add_edge("resolve_auth", "api_caller")
    g.add_edge("api_caller",   "json_parser")
    g.add_edge("json_parser",  "response_logger")
    g.add_edge("response_logger", END)

    return g.compile()
