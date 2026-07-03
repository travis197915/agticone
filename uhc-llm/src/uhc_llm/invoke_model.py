"""High-level MODEL_REGISTRY invoke API (messages list in, content out)."""
from __future__ import annotations

from typing import Any

from .registry import get_model_spec


def invoke_model(
    model_name: str,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int = 4096,
    json_mode: bool = False,
    agent_name: str = "",
) -> dict[str, Any]:
    """Invoke one registry model by key.

    Example::

        os.environ["LLM_BACKEND"] = "registry"
        os.environ["MODEL_REGISTRY"] = json.dumps({...})
        os.environ["AUTH_URL"] = "https://login.microsoftonline.com/.../oauth2/v2.0/token"
        os.environ["SCOPE"] = "https://api.uhg.com/.default"
        os.environ["CLIENT_ID"] = "..."
        os.environ["CLIENT_SECRET"] = "..."

        result = invoke_model("gpt-5-mini", messages, max_tokens=200)
        print(result["content"])
    """
    from .gateway import invoke_registry_chat

    spec = get_model_spec(model_name)
    content, inp, out = invoke_registry_chat(
        spec,
        messages=messages,
        max_tokens=max_tokens,
        json_mode=json_mode,
        agent_name=agent_name or model_name,
    )
    return {
        "model": model_name,
        "deployment": spec.deployment,
        "kind": spec.kind,
        "content": content,
        "prompt_tokens": inp,
        "completion_tokens": out,
    }
