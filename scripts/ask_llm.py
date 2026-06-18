#!/usr/bin/env python3
"""Send a prompt to a MODEL_REGISTRY model (parity with reference ask_llm.py).

Usage::

    python scripts/ask_llm.py

Edit SYSTEM_PROMPT, USER_QUESTION, and MODEL_NAME below.

Startup sequence (matches reference):
  1. Loads .env
  2. Fetches secrets from Azure Key Vault into os.environ (when configured)
  3. Refreshes MODEL_REGISTRY from env
  4. Calls invoke_model()
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

# ── EDIT THESE ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = "You are a helpful assistant."
USER_QUESTION = "What is the capital of France?"
MODEL_NAME = "gpt-5-mini"

# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        load_dotenv(str(env_file), override=False)
        print(f"Loaded .env from {env_file}")

    from uhc_llm import bootstrap_llm_secrets, invoke_model, refresh_model_registry
    from uhc_llm.gateway import gateway_auth_source
    from uhc_llm.keyvault_loader import keyvault_configured

    if keyvault_configured() or (_REPO_ROOT / ".env.stg").exists():
        print("Loading secrets from Azure Key Vault...")
        bootstrap_llm_secrets()
        print("Secrets loaded.\n")
    else:
        refresh_model_registry()

    from uhc_llm.registry import load_model_registry

    registry = load_model_registry()
    available = sorted(registry.keys())
    print(f"Available models: {available}")
    print(f"Auth: {gateway_auth_source()}")

    if MODEL_NAME not in registry:
        print(f"ERROR: Model {MODEL_NAME!r} not found. Pick from: {available}")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"System prompt : {SYSTEM_PROMPT[:80]}")
    print(f"Question      : {USER_QUESTION}")
    print(f"Model         : {MODEL_NAME}")
    print(f"{'=' * 60}\n")

    result = invoke_model(
        MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_QUESTION},
        ],
        max_tokens=200,
    )

    print(f"Model used: {result.get('model')}\n")
    print("Answer:")
    print("-" * 60)
    print(result.get("content") or "(no content returned)")
    print("-" * 60)


if __name__ == "__main__":
    main()
