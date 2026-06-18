"""CLI: ``uhc-llm-run-registry`` — smoke-test registry + OAuth from .env."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / ".env"
        if candidate.is_file():
            from dotenv import load_dotenv

            load_dotenv(candidate, override=False)
            return


def main() -> None:
    _load_dotenv()
    os.environ.setdefault("LLM_BACKEND", "registry")

    if not os.environ.get("MODEL_REGISTRY_JSON") and not os.environ.get("MODEL_REGISTRY"):
        print(
            "Set MODEL_REGISTRY_JSON or MODEL_REGISTRY in .env first.",
            file=sys.stderr,
        )
        sys.exit(1)

    model_key = os.environ.get("LLM_MODEL") or os.environ.get("REGISTRY_TEST_MODEL", "gpt-5-mini")
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {
            "role": "user",
            "content": os.environ.get(
                "REGISTRY_TEST_PROMPT",
                "Write a 1-sentence summary of Azure Container Apps.",
            ),
        },
    ]

    from uhc_llm import invoke_model
    from uhc_llm.gateway import gateway_auth_source
    from uhc_llm.registry import registry_profile_name

    print(f"registry={registry_profile_name()!r} auth={gateway_auth_source()!r} model={model_key!r}")
    result = invoke_model(model_key, messages, max_tokens=int(os.environ.get("REGISTRY_TEST_MAX_TOKENS", "200")))
    print(json.dumps({"model": result["model"], "content": result["content"]}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
