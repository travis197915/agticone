"""Config for the rule engine.

Reads the same `.env` as the rest of the repo. Only the LLM provider keys and
model names are needed; everything else (Postgres, Redis, Neo4j) is reached
through Django's ORM / agent_tools, so we don't reconfigure those here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _load_env() -> None:
    # Walk up looking for a repo-root .env, same trick uhc-api-agent uses.
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / ".env"
        if candidate.exists():
            load_dotenv(candidate, override=False)
            return


@dataclass(frozen=True)
class EngineConfig:
    anthropic_api_key: str
    anthropic_model: str
    openai_api_key: str
    openai_model: str
    llm_max_tokens: int = 4096
    llm_retries: int = 2
    # When true, EVALUATE-phase tool bindings are invoked lazily per step inside
    # execute_shapes (only for steps the router actually reaches) instead of all
    # upfront in run_tools. Default OFF: we now pre-fetch every tool once,
    # deduped + in parallel, in run_tools so the results are already in context
    # for rule evaluation (and rule evaluation can run concurrently without
    # making live tool calls). Set RULE_ENGINE_LAZY_TOOLS=1 to restore the old
    # per-step lazy behaviour.
    lazy_tools: bool = False
    # Upper bound on concurrent tool invocations during the pre-fetch and on
    # concurrent per-SOP rule-evaluation cursors. Tune via env.
    tool_prefetch_workers: int = 8
    sop_eval_workers: int = 8


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def get_config() -> EngineConfig:
    _load_env()
    return EngineConfig(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
        llm_max_tokens=int(os.environ.get("RULE_ENGINE_MAX_TOKENS", "4096")),
        llm_retries=int(os.environ.get("RULE_ENGINE_RETRIES", "2")),
        lazy_tools=_env_bool("RULE_ENGINE_LAZY_TOOLS", False),
        tool_prefetch_workers=int(os.environ.get("RULE_ENGINE_TOOL_WORKERS", "8")),
        sop_eval_workers=int(os.environ.get("RULE_ENGINE_SOP_WORKERS", "8")),
    )
