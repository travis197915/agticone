"""SOP field-mapping resolver.

Bridges the two repo-root YAMLs into the rule engine:

* ``yaml/sop_field_mapping.yaml`` — maps each canonical SOP business field
  ("Provider TIN", "Received Date", …) to the concrete field names used by
  each source system (DOC360 ontology keys, FACETS columns, CBS/CBD/NPI).
* ``yaml/claim_ontology.yaml`` — CMS-1500 ontology used to normalise a parsed
  claim image (DOC360). Loaded here mainly so alias labels can resolve to the
  same canonical DOC360 keys the mapping references.

The resolver deep-searches the claim dict + any tool results for the mapped
source keys and returns the first concrete value it finds, preferring the
authoritative FACETS columns over DOC360 image fields.

Design notes:
* Fully additive. If the YAMLs are missing or resolve nothing, callers get an
  empty dict and behaviour is unchanged (backward compatible).
* Path resolution: ``SOP_YAML_DIR`` env override → Django ``BASE_DIR/yaml`` →
  walk up from this file looking for ``yaml/sop_field_mapping.yaml``.
"""
from __future__ import annotations

import functools
import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Placeholder tokens in the mapping that are not real source keys.
_PLACEHOLDERS = {"NOT APPLICABLE", "N/A", "NA", ""}
# Preference order: structured system-of-record first, image last.
_SYSTEM_ORDER = ("FACETS", "DOC360", "CBS", "CBD", "NPI")


def _yaml_dir() -> Path:
    env = os.environ.get("SOP_YAML_DIR")
    if env:
        return Path(env)
    try:  # Django is the normal runtime host for the engine.
        from django.conf import settings  # type: ignore

        base = Path(getattr(settings, "BASE_DIR", ""))
        if base and (base / "yaml" / "sop_field_mapping.yaml").exists():
            return base / "yaml"
    except Exception:
        pass
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "yaml" / "sop_field_mapping.yaml").exists():
            return parent / "yaml"
    return Path("yaml")


@functools.lru_cache(maxsize=1)
def _load() -> tuple[dict[str, Any], dict[str, Any]]:
    d = _yaml_dir()
    mapping: dict[str, Any] = {}
    ontology: dict[str, Any] = {}
    try:
        with open(d / "sop_field_mapping.yaml") as fh:
            mapping = yaml.safe_load(fh) or {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("field_mapping: could not load sop_field_mapping.yaml: %s", exc)
    try:
        with open(d / "claim_ontology.yaml") as fh:
            ontology = yaml.safe_load(fh) or {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("field_mapping: could not load claim_ontology.yaml: %s", exc)
    return mapping, ontology


def _is_placeholder(key: str) -> bool:
    k = (key or "").strip()
    return k.upper() in _PLACEHOLDERS or k.upper().startswith("FIND FROM")


def _deep_find(obj: Any, key: str) -> Any:
    """Breadth-first search for the first non-empty value stored under ``key``
    anywhere in a nested dict/list structure."""
    stack: list[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur:
                val = cur[key]
                if val not in (None, "", [], {}):
                    return val
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def resolve_sop_fields(
    claim: dict[str, Any],
    tool_context: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve canonical SOP fields from the claim + tool results.

    Returns ``{sop_field: {"value": <value>, "source": "<SYSTEM>:<key>"}}``
    for every mapped field a concrete value was found for. Empty dict when the
    mapping YAML is unavailable or nothing resolves.
    """
    mapping, _ = _load()
    mappings = (mapping or {}).get("mappings") or {}
    if not mappings:
        return {}

    search_spaces: list[Any] = [claim or {}]
    for tc in tool_context or []:
        if isinstance(tc, dict) and tc.get("result") is not None:
            search_spaces.append(tc["result"])

    resolved: dict[str, dict[str, Any]] = {}
    for sop_field, src in mappings.items():
        if not isinstance(src, dict):
            continue
        value = None
        where = None
        for system in _SYSTEM_ORDER:
            cols = src.get(system) or []
            for col in cols:
                if _is_placeholder(col):
                    continue
                for space in search_spaces:
                    found = _deep_find(space, col)
                    if found is not None:
                        value, where = found, f"{system}:{col}"
                        break
                if value is not None:
                    break
            if value is not None:
                break
        if value is not None:
            resolved[str(sop_field).strip()] = {"value": value, "source": where}
    return resolved


def format_mapped_fields_block(resolved: dict[str, dict[str, Any]]) -> str:
    """Render resolved fields as a compact prompt block, or '' when empty."""
    if not resolved:
        return ""
    lines = [
        f"- {field}: {info['value']}   (source: {info['source']})"
        for field, info in resolved.items()
    ]
    return (
        "MAPPED SOP FIELDS (resolved from the claim via sop_field_mapping.yaml;\n"
        "these are the authoritative, system-of-record values to reason over)\n"
        "----------------------------------------------------------------------\n"
        + "\n".join(lines)
    )
