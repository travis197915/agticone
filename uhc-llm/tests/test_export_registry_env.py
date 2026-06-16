"""Tests for export_registry_env helper (Option A .env builder)."""
from __future__ import annotations

import json
from pathlib import Path

from uhc_llm.export_registry_env import compact_registry_json, format_dotenv_line, main


def test_format_dotenv_line_from_example(tmp_path):
    src = tmp_path / "registry.json"
    src.write_text(json.dumps({
        "mini": {
            "kind": "openai_compat",
            "endpoint": "https://example",
            "deployment": "mini",
        }
    }), encoding="utf-8")
    line = format_dotenv_line(src)
    assert line.startswith("MODEL_REGISTRY_JSON='")
    assert line.endswith("'")
    assert '"mini"' in line


def test_main_validate_flag(tmp_path, capsys):
    src = tmp_path / "registry.json"
    src.write_text(json.dumps({
        "mini": {
            "kind": "openai_compat",
            "endpoint": "https://example",
            "deployment": "mini",
        }
    }), encoding="utf-8")
    rc = main([str(src), "--validate"])
    assert rc == 0
    out = capsys.readouterr()
    assert out.out.startswith("MODEL_REGISTRY_JSON='")
    assert "ok: 1 models" in out.err


def test_compact_matches_bundled_example():
    example = (
        Path(__file__).resolve().parents[1]
        / "config" / "registries" / "uhg-gateway.example.json"
    )
    compact = compact_registry_json(example)
    parsed = json.loads(compact)
    assert set(parsed) == {"gpt-5-mini", "llama", "opus"}
