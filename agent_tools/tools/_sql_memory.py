"""
In-memory shim that replaces pymssql for the two SQL-backed tools
(``save_sop_step`` and ``check_cross_prevalence_billing``).

Active when ``AGENT_TOOLS_SQL_BACKEND=memory`` (the default in mock mode).
Stores rows in a per-table list keyed by a synthetic auto-incrementing id.
"""
from __future__ import annotations

import itertools
import os
import threading
from typing import Any


_LOCK = threading.Lock()
_TABLES: dict[str, list[dict[str, Any]]] = {}
_AUTOINC = itertools.count(1)


def using_memory() -> bool:
    return (os.environ.get("AGENT_TOOLS_SQL_BACKEND", "memory") or "memory").strip().lower() == "memory"


def insert(table: str, row: dict[str, Any]) -> int:
    """Insert a row and return its synthetic id."""
    row_id = next(_AUTOINC)
    record = dict(row, id=row_id)
    with _LOCK:
        _TABLES.setdefault(table, []).append(record)
    return row_id


def update(table: str, row_id: int, patch: dict[str, Any]) -> bool:
    with _LOCK:
        rows = _TABLES.get(table, [])
        for row in rows:
            if row.get("id") == row_id:
                row.update(patch)
                return True
    return False


def upsert(table: str, key_fields: list[str], row: dict[str, Any]) -> tuple[int, str]:
    """Upsert by composite key. Returns ``(id, "inserted" | "updated")``."""
    with _LOCK:
        rows = _TABLES.setdefault(table, [])
        for existing in rows:
            if all(existing.get(k) == row.get(k) for k in key_fields):
                existing.update(row)
                return existing["id"], "updated"
        row_id = next(_AUTOINC)
        rows.append(dict(row, id=row_id))
        return row_id, "inserted"


def select(table: str, **filters: Any) -> list[dict[str, Any]]:
    with _LOCK:
        rows = list(_TABLES.get(table, []))
    if not filters:
        return rows
    return [r for r in rows if all(r.get(k) == v for k, v in filters.items())]


def clear() -> None:
    with _LOCK:
        _TABLES.clear()
