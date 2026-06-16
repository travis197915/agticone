"""Load MODEL_REGISTRY / AGENT_MODEL_MAP from JSON files or inline env JSON."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .paths import profile_path


JSON_COMPATIBLE_KINDS = frozenset({"azure_openai", "openai_compat"})


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str
    endpoint: str
    deployment: str
    api_version: str = ""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _looks_like_json_object(raw: str) -> bool:
    s = raw.lstrip()
    return s.startswith("{") or s.startswith("[")


def _parse_json_object(raw: str, label: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid {label} JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return parsed


def _load_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {label} file {path}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{label} file {path} must contain a JSON object")
    return parsed


def _resolve_config_source(env_value: str, *, subdir: str, label: str) -> tuple[dict[str, Any], str]:
    """Return (parsed_object, source_description) for one env setting."""
    raw = env_value.strip()
    if not raw:
        return {}, ""

    if _looks_like_json_object(raw):
        return _parse_json_object(raw, label), f"{label}:inline"

    path = profile_path(raw, subdir)
    return _load_json_file(path, label), f"{label}:{path}"


def _build_model_specs(entries: dict[str, Any]) -> dict[str, ModelSpec]:
    specs: dict[str, ModelSpec] = {}
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            raise RuntimeError(f"MODEL_REGISTRY[{name!r}] must be an object")
        kind = str(entry.get("kind") or "").strip()
        endpoint = str(entry.get("endpoint") or "").strip()
        deployment = str(entry.get("deployment") or "").strip()
        if not kind or not endpoint or not deployment:
            raise RuntimeError(
                f"MODEL_REGISTRY[{name!r}] requires kind, endpoint, deployment"
            )
        specs[str(name)] = ModelSpec(
            name=str(name),
            kind=kind,
            endpoint=endpoint.rstrip("/"),
            deployment=deployment,
            api_version=str(entry.get("api_version") or "").strip(),
        )
    return specs


@lru_cache(maxsize=1)
def load_model_registry() -> dict[str, ModelSpec]:
    """Load models from ``MODEL_REGISTRY`` profile name, file path, or inline JSON."""
    raw = _env("MODEL_REGISTRY")
    if not raw:
        return {}
    data, _source = _resolve_config_source(
        raw, subdir="registries", label="MODEL_REGISTRY",
    )
    return _build_model_specs(data)


@lru_cache(maxsize=1)
def load_agent_model_map() -> dict[str, str]:
    """Load agent→model mapping from ``AGENT_MODEL_MAP`` profile, file, or inline JSON."""
    raw = _env("AGENT_MODEL_MAP")
    if not raw:
        return {}
    data, _source = _resolve_config_source(
        raw, subdir="agent_maps", label="AGENT_MODEL_MAP",
    )
    return {str(k): str(v) for k, v in data.items()}


def registry_profile_name() -> str:
    """Human-readable selector currently configured for MODEL_REGISTRY."""
    return _env("MODEL_REGISTRY") or ""


def agent_map_profile_name() -> str:
    """Human-readable selector currently configured for AGENT_MODEL_MAP."""
    return _env("AGENT_MODEL_MAP") or ""


def global_registry_model_name() -> str:
    """App-wide model key for registry mode (same model for every agent).

    Set ``LLM_MODEL`` (or legacy ``REGISTRY_DEFAULT_MODEL``) to a key that
    exists in the active ``MODEL_REGISTRY`` profile. When set, per-agent
    ``AGENT_MODEL_MAP`` entries are ignored.
    """
    return _env("LLM_MODEL") or _env("REGISTRY_DEFAULT_MODEL")


def resolve_registry_model_name(agent_name: str) -> str:
    """Resolve which registry model key to use for an LLM call.

    Priority:
    1. ``LLM_MODEL`` / ``REGISTRY_DEFAULT_MODEL`` — one model for the whole app
    2. ``AGENT_MODEL_MAP`` entry for ``agent_name`` (optional per-agent override)
    3. ``AGENT_MODEL_MAP`` ``__default__``
    4. Sole model in ``MODEL_REGISTRY`` when the profile defines exactly one
    """
    registry = load_model_registry()
    if not registry:
        raise RuntimeError(
            "MODEL_REGISTRY is not set but registry backend is active. "
            "Set MODEL_REGISTRY to a profile name (e.g. uhg-gateway) or a .json path."
        )

    model_name = global_registry_model_name()
    if not model_name:
        mapping = load_agent_model_map()
        model_name = mapping.get(agent_name) or mapping.get("__default__")
    if not model_name:
        if len(registry) == 1:
            return next(iter(registry))
        raise RuntimeError(
            f"No model configured for agent {agent_name!r}. "
            f"Set LLM_MODEL to a key from MODEL_REGISTRY profile "
            f"{registry_profile_name()!r} (recommended for app-wide routing), "
            f"or set AGENT_MODEL_MAP. Known models: {sorted(registry)}"
        )
    if model_name not in registry:
        raise RuntimeError(
            f"Resolved model {model_name!r} for agent {agent_name!r} "
            f"is not in MODEL_REGISTRY profile {registry_profile_name()!r}. "
            f"Known models: {sorted(registry)}"
        )
    return model_name


def get_model_spec(model_name: str) -> ModelSpec:
    registry = load_model_registry()
    spec = registry.get(model_name)
    if spec is None:
        raise RuntimeError(
            f"Model {model_name!r} not found in MODEL_REGISTRY profile "
            f"{registry_profile_name()!r}. Known: {sorted(registry)}"
        )
    return spec
