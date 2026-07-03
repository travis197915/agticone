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
    llm_model: str = ""  # app-wide registry model key (LLM_MODEL env)
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
    # Per-claim persistent context (ClaimMemory). When enabled, every run loads
    # the prior context for (claim_id, workflow), reuses EVALUATE-phase tool
    # results within the TTL, injects prior verdicts into rule prompts
    # (awareness-only), and resolves live-vs-prior disagreements per the
    # conflict policy. Disabled -> exact pre-memory behaviour.
    claim_memory_enabled: bool = True
    # "prior_wins": on structured drift (matched/skipped flip) with unchanged
    # claim data, adopt the prior run's verdict + reasoning and keep the live
    # output in RuleEvaluation.live_result. "live_wins": keep the live verdict
    # and only record the drift entry.
    claim_memory_conflict_policy: str = "prior_wins"
    claim_memory_tool_ttl_hours: int = 24
    # Debug observability: when true, every rule_evaluated SSE event carries a
    # `prior_context` record showing exactly what memory context was injected
    # into that rule's prompt (or a reason code when nothing was), and the
    # same record persists to RuleEvaluation.injected_context.
    claim_memory_stream_context: bool = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def get_config() -> EngineConfig:
    from uhc_llm.backend import DEFAULT_ANTHROPIC_MODEL, DEFAULT_OPENAI_MODEL

    _load_env()
    return EngineConfig(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_model=os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        llm_max_tokens=int(os.environ.get("RULE_ENGINE_MAX_TOKENS", "4096")),
        llm_model=os.environ.get("LLM_MODEL", "") or os.environ.get("REGISTRY_DEFAULT_MODEL", ""),
        lazy_tools=_env_bool("RULE_ENGINE_LAZY_TOOLS", False),
        tool_prefetch_workers=int(os.environ.get("RULE_ENGINE_TOOL_WORKERS", "8")),
        sop_eval_workers=int(os.environ.get("RULE_ENGINE_SOP_WORKERS", "8")),
        claim_memory_enabled=_env_bool("CLAIM_MEMORY_ENABLED", True),
        claim_memory_conflict_policy=os.environ.get(
            "CLAIM_MEMORY_CONFLICT_POLICY", "prior_wins").strip().lower(),
        claim_memory_tool_ttl_hours=int(
            os.environ.get("CLAIM_MEMORY_TOOL_TTL_HOURS", "24")),
        claim_memory_stream_context=_env_bool(
            "CLAIM_MEMORY_STREAM_CONTEXT", False),
    )
