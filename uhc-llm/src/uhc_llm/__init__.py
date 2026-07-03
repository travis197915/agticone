"""Env-driven LLM routing for UHC services."""

from .backend import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_OPENAI_MODEL,
    apply_job_llm_env,
    get_llm_backend,
    is_registry_backend,
    resolve_api_key_model,
    resolve_ingestion_job_llm,
)
from .invoke_model import invoke_model
from .keyvault_loader import bootstrap_llm_secrets, keyvault_configured, load_secrets_into_env
from .registry import (
    agent_map_profile_name,
    global_registry_model_name,
    load_agent_model_map,
    load_model_registry,
    refresh_model_registry,
    registry_profile_name,
    resolve_pdf_registry_model_name,
    resolve_registry_model_name,
)
from .router import LLMResponse, describe_target, invoke_chat, invoke_pdf, invoke_prompt

__all__ = [
    "DEFAULT_ANTHROPIC_MODEL",
    "DEFAULT_OPENAI_MODEL",
    "LLMResponse",
    "agent_map_profile_name",
    "apply_job_llm_env",
    "bootstrap_llm_secrets",
    "describe_target",
    "get_llm_backend",
    "global_registry_model_name",
    "invoke_chat",
    "invoke_model",
    "invoke_pdf",
    "invoke_prompt",
    "is_registry_backend",
    "keyvault_configured",
    "load_agent_model_map",
    "load_model_registry",
    "load_secrets_into_env",
    "refresh_model_registry",
    "registry_profile_name",
    "resolve_api_key_model",
    "resolve_ingestion_job_llm",
    "resolve_registry_model_name",
    "resolve_pdf_registry_model_name",
]
