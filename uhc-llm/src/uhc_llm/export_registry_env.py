#!/usr/bin/env python3
"""Emit a dotenv-ready MODEL_REGISTRY_JSON line from a registry JSON file.

Usage::

    # After copying and editing the example registry:
    cp uhc-llm/config/registries/uhg-gateway.example.json /tmp/my-registry.json
    # edit /tmp/my-registry.json with real gateway endpoints
    uhc-llm-export-registry /tmp/my-registry.json >> .env

    # Or print to stdout for copy/paste:
    uhc-llm-export-registry uhc-llm/config/registries/uhg-gateway.example.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def compact_registry_json(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object of model specs")
    return json.dumps(data, separators=(",", ":"), ensure_ascii=True)


def format_dotenv_line(path: Path, *, var_name: str = "MODEL_REGISTRY_JSON") -> str:
    payload = compact_registry_json(path)
    return f"{var_name}='{payload}'"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print MODEL_REGISTRY_JSON='...' for Option A (.env) setup.",
    )
    parser.add_argument(
        "json_file",
        type=Path,
        help="Registry JSON file (e.g. uhc-llm/config/registries/uhg-gateway.example.json)",
    )
    parser.add_argument(
        "--var",
        default="MODEL_REGISTRY_JSON",
        help="Env var name to emit (default: MODEL_REGISTRY_JSON)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Parse through uhc_llm.load_model_registry after printing",
    )
    args = parser.parse_args(argv)

    if not args.json_file.is_file():
        print(f"error: file not found: {args.json_file}", file=sys.stderr)
        return 1

    try:
        line = format_dotenv_line(args.json_file, var_name=args.var)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(line)

    if args.validate:
        import os
        os.environ[args.var] = compact_registry_json(args.json_file)
        os.environ.pop("MODEL_REGISTRY", None)
        os.environ.pop("MODEL_REGISTRY_FILE", None)
        from uhc_llm.registry import load_model_registry
        load_model_registry.cache_clear()
        specs = load_model_registry()
        print(
            f"# ok: {len(specs)} models → {sorted(specs)}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
