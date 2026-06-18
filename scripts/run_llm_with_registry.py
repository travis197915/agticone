#!/usr/bin/env python3
"""Smoke-test MODEL_REGISTRY + OAuth against the UHG AI gateway.

Usage (from repo root, with uhc-llm installed editable)::

    cp .env.example .env   # fill AUTH_URL, CLIENT_ID, CLIENT_SECRET, SCOPE, MODEL_REGISTRY_JSON
    python scripts/run_llm_with_registry.py

Or set env inline like the reference ``run_llm_with_registry.py`` example.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    root = Path(__file__).resolve().parents[1]
    env_file = root / ".env"
    if env_file.is_file():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)


def main() -> None:
    _load_dotenv()

    # Minimal inline registry (override with MODEL_REGISTRY_JSON in .env)
    if not os.environ.get("MODEL_REGISTRY_JSON") and not os.environ.get("MODEL_REGISTRY"):
        model_registry = {
            "test-model": {
                "kind": "openai_compat",
                "endpoint": "https://api.uhg.com/api/cloud/api-management/ai-gateway/1.0",
                "deployment": "llama-3-3_70b-instruct",
            }
        }
        os.environ.setdefault("MODEL_REGISTRY", json.dumps(model_registry))
        model_key = "test-model"
    else:
        model_key = os.environ.get("LLM_MODEL", "gpt-5-mini")

    os.environ.setdefault("LLM_BACKEND", "registry")

    # OAuth (preferred) — or set AI_GATEWAY_API_KEY in .env instead
    # os.environ.setdefault("AUTH_URL", "https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token")
    # os.environ.setdefault("SCOPE", "https://api.uhg.com/.default")
    # os.environ.setdefault("CLIENT_ID", "your-client-id")
    # os.environ.setdefault("CLIENT_SECRET", "your-client-secret")
    # os.environ.setdefault("PROJECT_ID", "optional-project-uuid")

    from uhc_llm import invoke_model
    from uhc_llm.gateway import gateway_auth_source
    from uhc_llm.registry import registry_profile_name

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {
            "role": "user",
            "content": "Write a 1-sentence summary of Azure Container Apps.",
        },
    ]

    print(f"registry={registry_profile_name()!r} auth={gateway_auth_source()!r} model={model_key!r}")
    result = invoke_model(model_key, messages, max_tokens=200)

    print("Model:", result["model"])
    print("Deployment:", result["deployment"])
    print("Content:", result["content"])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
