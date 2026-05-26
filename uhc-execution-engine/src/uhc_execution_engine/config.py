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
    )
