"""Env-driven LLM routing for UHC services."""

from .backend import apply_job_llm_env, get_llm_backend, is_registry_backend
from .registry import (
    agent_map_profile_name,
    global_registry_model_name,
    load_agent_model_map,
    load_model_registry,
    registry_profile_name,
    resolve_registry_model_name,
    resolve_pdf_registry_model_name,
)
from .router import LLMResponse, invoke_prompt

__all__ = [
    "LLMResponse",
    "agent_map_profile_name",
    "apply_job_llm_env",
    "get_llm_backend",
    "global_registry_model_name",
    "invoke_prompt",
    "is_registry_backend",
    "load_agent_model_map",
    "load_model_registry",
    "registry_profile_name",
    "resolve_registry_model_name",
    "resolve_pdf_registry_model_name",
]
