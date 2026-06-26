"""Message normalization and multimodal PDF helpers (registry + api_key)."""
from __future__ import annotations

import base64
import io
from typing import Any


def normalize_chat_messages(
    messages: list[dict[str, Any]] | None,
    *,
    prompt: str = "",
) -> list[dict[str, str]]:
    if messages:
        return [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in messages
        ]
    if prompt:
        return [{"role": "user", "content": prompt}]
    return [{"role": "user", "content": ""}]


def split_anthropic_system_messages(
    messages: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]]]:
    """Move ``system`` role content to Anthropic's ``system`` parameter."""
    system_parts: list[str] = []
    chat: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        chat.append({"role": role, "content": content})
    if not chat:
        chat = [{"role": "user", "content": ""}]
    system = "\n\n".join(system_parts) if system_parts else None
    return system, chat


def pdf_document_blocks(prompt: str, pdf_b64_list: list[str]) -> list[dict[str, Any]]:
    """Build Anthropic multimodal blocks: PDF document slices + trailing text prompt."""
    blocks: list[dict[str, Any]] = []
    for b64 in pdf_b64_list:
        if not b64:
            continue
        blocks.append({
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": b64,
            },
        })
    blocks.append({"type": "text", "text": prompt})
    return blocks


def extract_pdf_text_from_b64(
    pdf_b64_list: list[str],
    *,
    max_chars: int = 120_000,
) -> str:
    """Best-effort plain-text extraction from base64 PDF slices."""
    from pypdf import PdfReader

    parts: list[str] = []
    total = 0
    for b64 in pdf_b64_list:
        if not b64:
            continue
        try:
            reader = PdfReader(io.BytesIO(base64.b64decode(b64)))
        except Exception:
            continue
        for page in reader.pages:
            txt = (page.extract_text() or "").strip()
            if not txt:
                continue
            parts.append(txt)
            total += len(txt)
            if total >= max_chars:
                break
        if total >= max_chars:
            break
    return "\n\n".join(parts)[:max_chars]
