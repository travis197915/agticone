"""Locate bundled or repo-local LLM config files."""
from __future__ import annotations

import os
import sys
from pathlib import Path


_PKG_ROOT = Path(__file__).resolve().parents[2]


def config_root() -> Path:
    """Root directory for registry / agent-map JSON files."""
    override = os.environ.get("UHC_LLM_CONFIG_DIR", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_dir():
            raise RuntimeError(f"UHC_LLM_CONFIG_DIR is not a directory: {path}")
        return path

    bundled = _PKG_ROOT / "config"
    if bundled.is_dir():
        return bundled

    share = Path(sys.prefix) / "share" / "uhc-llm" / "config"
    if share.is_dir():
        return share

    raise RuntimeError(
        "LLM config directory not found. Set UHC_LLM_CONFIG_DIR or install "
        "uhc-llm with bundled config/registries and config/agent_maps."
    )


def profile_path(selector: str, subdir: str) -> Path:
    """Resolve a profile name or file path under ``config/<subdir>/``."""
    raw = selector.strip()
    if not raw:
        raise RuntimeError(f"Empty selector for config/{subdir}")

    candidate = Path(raw).expanduser()
    if candidate.is_file():
        return candidate

    root = config_root()
    if candidate.suffix == ".json" and not candidate.is_absolute():
        from_root = root / subdir / candidate.name
        if from_root.is_file():
            return from_root

    named = root / subdir / f"{raw}.json"
    if named.is_file():
        return named

    raise RuntimeError(
        f"Could not resolve config/{subdir} profile {raw!r}. "
        f"Tried: {candidate}, {named}. "
        f"Config root: {root}"
    )
