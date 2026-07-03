#!/usr/bin/env python3
"""Send a prompt to a MODEL_REGISTRY model (parity with reference ask_llm.py).

Usage::

    python scripts/ask_llm.py
    python scripts/ask_llm.py --model opus --pdf /path/to/doc.pdf

Edit SYSTEM_PROMPT, USER_QUESTION, and MODEL_NAME below, or pass CLI flags.

Startup sequence (matches reference):
  1. Loads .env
  2. Fetches secrets from Azure Key Vault into os.environ (when configured)
  3. Refreshes MODEL_REGISTRY from env
  4. Calls invoke_model() or invoke_registry_messages() for PDF
"""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

# ── EDIT THESE ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = "You are a helpful assistant."
USER_QUESTION = "What is the capital of France?"
MODEL_NAME = "opus"

# ─────────────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask registry model with optional PDF input")
    parser.add_argument("--model", default=MODEL_NAME, help="Registry model key (default: opus)")
    parser.add_argument("--system", default=SYSTEM_PROMPT, help="System prompt")
    parser.add_argument("--question", default=USER_QUESTION, help="User question/prompt")
    parser.add_argument(
        "--pdf",
        default="",
        help="Path to a local PDF file. When set, runs a multimodal document call.",
    )
    parser.add_argument("--max-tokens", type=int, default=400, help="Max output tokens")
    return parser.parse_args()


def _pdf_to_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def main() -> None:
    args = _parse_args()

    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        load_dotenv(str(env_file), override=False)
        print(f"Loaded .env from {env_file}")

    from uhc_llm import bootstrap_llm_secrets, refresh_model_registry
    from uhc_llm.gateway import gateway_auth_source
    from uhc_llm.keyvault_loader import keyvault_configured
    from uhc_llm.registry import load_model_registry
    from uhc_llm.router import invoke_chat, invoke_pdf

    if keyvault_configured() or (_REPO_ROOT / ".env.stg").exists():
        print("Loading secrets from Azure Key Vault...")
        bootstrap_llm_secrets()
        print("Secrets loaded.\n")
    else:
        refresh_model_registry()

    registry = load_model_registry()
    available = sorted(registry.keys())
    print(f"Available models: {available}")
    print(f"Auth: {gateway_auth_source()}")

    model_name = args.model
    system_prompt = args.system
    user_question = args.question

    if model_name not in registry:
        print(f"ERROR: Model {model_name!r} not found. Pick from: {available}")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"System prompt : {system_prompt[:80]}")
    print(f"Question      : {user_question}")
    print(f"Model         : {model_name}")
    if args.pdf:
        print(f"PDF           : {args.pdf}")
    print(f"{'=' * 60}\n")

    if args.pdf:
        pdf_path = Path(args.pdf).expanduser().resolve()
        if not pdf_path.is_file():
            print(f"ERROR: PDF not found: {pdf_path}")
            sys.exit(1)
        combined_prompt = (
            f"System instruction:\n{system_prompt}\n\n"
            f"User question:\n{user_question}"
        )
        resp = invoke_pdf(
            agent_name="ask_llm_pdf",
            prompt=combined_prompt,
            pdf_b64_list=[_pdf_to_b64(pdf_path)],
            max_tokens=args.max_tokens,
            model_name=model_name,
        )
        result = {
            "model": resp.model,
            "content": resp.content,
            "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens,
        }
    else:
        resp = invoke_chat(
            agent_name="ask_llm",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_question},
            ],
            max_tokens=args.max_tokens,
            model_name=model_name,
        )
        result = {
            "model": resp.model,
            "content": resp.content,
            "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens,
        }

    print(f"Model used: {result.get('model')}\n")
    print("Answer:")
    print("-" * 60)
    print(result.get("content") or "(no content returned)")
    print("-" * 60)


if __name__ == "__main__":
    main()
