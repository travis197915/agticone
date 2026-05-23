"""Tiny fixture loader for the mock views."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@lru_cache(maxsize=64)
def load(name: str) -> Any:
    path = FIXTURES / name
    return json.loads(path.read_text(encoding="utf-8"))
