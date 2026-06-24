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
import io
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


def _extract_pdf_text(path: Path, *, max_chars: int = 30000) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required for --pdf with non-bedrock models") from exc

    reader = PdfReader(io.BytesIO(path.read_bytes()))
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        txt = (page.extract_text() or "").strip()
        if not txt:
            continue
        parts.append(txt)
        total += len(txt)
        if total >= max_chars:
            break
    return "\n\n".join(parts)[:max_chars]


def _pdf_content_blocks(prompt: str, pdf_b64: str) -> list[dict]:
    return [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_b64,
            },
        },
        {"type": "text", "text": prompt},
    ]


def main() -> None:
    args = _parse_args()

    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        load_dotenv(str(env_file), override=False)
        print(f"Loaded .env from {env_file}")

    from uhc_llm import bootstrap_llm_secrets, invoke_model, refresh_model_registry
    from uhc_llm.gateway import gateway_auth_source, invoke_registry_messages
    from uhc_llm.keyvault_loader import keyvault_configured
    from uhc_llm.registry import get_model_spec, load_model_registry

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
        spec = get_model_spec(model_name)
        if spec.kind == "bedrock_claude":
            combined_prompt = (
                f"System instruction:\n{system_prompt}\n\n"
                f"User question:\n{user_question}"
            )
            content, inp, out = invoke_registry_messages(
                spec,
                content_blocks=_pdf_content_blocks(combined_prompt, _pdf_to_b64(pdf_path)),
                max_tokens=args.max_tokens,
                agent_name="ask_llm_pdf",
            )
            result = {
                "model": model_name,
                "content": content,
                "prompt_tokens": inp,
                "completion_tokens": out,
            }
        else:
            extracted = _extract_pdf_text(pdf_path)
            if not extracted:
                print("ERROR: Could not extract text from PDF for non-bedrock model.")
                sys.exit(1)
            print(
                f"NOTE: Model {model_name!r} (kind={spec.kind!r}) does not support native "
                "PDF document blocks; using extracted text fallback."
            )
            text_prompt = (
                f"System instruction:\n{system_prompt}\n\n"
                f"User question:\n{user_question}\n\n"
                "PDF_TEXT_EXTRACT:\n"
                f"{extracted}"
            )
            result = invoke_model(
                model_name,
                messages=[{"role": "user", "content": text_prompt}],
                max_tokens=args.max_tokens,
            )
            if not (result.get("content") or "").strip():
                extracted_short = extracted[:12000]
                retry_prompt = (
                    f"System instruction:\n{system_prompt}\n\n"
                    f"User question:\n{user_question}\n\n"
                    "PDF_TEXT_EXTRACT (short retry excerpt):\n"
                    f"{extracted_short}"
                )
                result = invoke_model(
                    model_name,
                    messages=[{"role": "user", "content": retry_prompt}],
                    max_tokens=args.max_tokens,
                )
    else:
        result = invoke_model(
            model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_question},
            ],
            max_tokens=args.max_tokens,
        )

    print(f"Model used: {result.get('model')}\n")
    print("Answer:")
    print("-" * 60)
    print(result.get("content") or "(no content returned)")
    print("-" * 60)


if __name__ == "__main__":
    main()
