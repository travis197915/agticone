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
def _load_files() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the mapping + ontology from the repo-root YAMLs (fallback source)."""
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


# In-process cache for the DB-backed mapping, keyed by a cheap watermark so
# edits made in another process (e.g. the web tier) become visible to this one
# (e.g. a Celery worker) without a restart. ``reset_cache`` clears it eagerly
# for same-process immediacy (wired to a post_save signal in agent_tools).
_DB_CACHE: dict[str, Any] = {}


def _db_watermark() -> tuple[int, str, int, str] | None:
    """Cheap change-detector for both config tables:
    ``(map_count, map_max_updated, ont_count, ont_max_updated)``.

    Returns ``None`` when Django/the tables are unavailable (standalone CLI,
    app not ready) so callers fall back to the YAMLs.
    """
    try:
        from django.db.models import Count, Max

        from agent_tools.models import ClaimOntologyField, SopFieldMapping
    except Exception:
        return None
    try:
        m = SopFieldMapping.objects.filter(is_active=True).aggregate(
            c=Count("id"), u=Max("updated_at")
        )
        o = ClaimOntologyField.objects.filter(is_active=True).aggregate(
            c=Count("id"), u=Max("updated_at")
        )
    except Exception:  # pragma: no cover - DB not migrated/reachable
        return None
    return (
        m.get("c") or 0, m["u"].isoformat() if m.get("u") else "",
        o.get("c") or 0, o["u"].isoformat() if o.get("u") else "",
    )


def _mapping_from_db() -> dict[str, Any]:
    """Build the ``{"mappings": {field: {systems}}}`` doc from active DB rows."""
    from agent_tools.models import SopFieldMapping

    rows = SopFieldMapping.objects.filter(is_active=True).values("sop_field", "systems")
    return {"mappings": {r["sop_field"]: (r["systems"] or {}) for r in rows}}


def _ontology_from_db() -> dict[str, Any]:
    """Build the ``{"namespaces": {ns: {field: {aliases}}}}`` doc from DB rows."""
    from agent_tools.models import ClaimOntologyField

    rows = ClaimOntologyField.objects.filter(is_active=True).values(
        "namespace", "canonical_field", "aliases"
    )
    namespaces: dict[str, Any] = {}
    for r in rows:
        ns = namespaces.setdefault(r["namespace"], {})
        ns[r["canonical_field"]] = {"aliases": r["aliases"] or []}
    return {"namespaces": namespaces}


# Memoised alias index, keyed by ``id(ontology)`` (ontology dicts are cached, so
# identity is stable until an edit rebuilds them).
_ALIAS_CACHE: dict[int, dict[str, list[str]]] = {}


def reset_cache() -> None:
    """Drop the in-process config caches (DB + file). Called by the
    ``agent_tools`` post_save/post_delete signal so UI edits take effect at
    once in this process; other processes pick the change up via the watermark."""
    _DB_CACHE.clear()
    _ALIAS_CACHE.clear()
    _load_files.cache_clear()


def _norm(s: Any) -> str:
    """Normalise a label for alias matching: upper-cased, single-spaced."""
    return " ".join(str(s or "").strip().upper().split())


def _alias_index_for(ontology: dict[str, Any]) -> dict[str, list[str]]:
    """Build (and memoise) ``normalised_term -> [equivalent raw terms]`` from the
    claim ontology, so a field-mapping source key can be expanded to every label
    the claim image might actually use for it."""
    cache_key = id(ontology)
    cached = _ALIAS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    idx: dict[str, list[str]] = {}
    namespaces = (ontology or {}).get("namespaces") or {}
    for fields in namespaces.values():
        if not isinstance(fields, dict):
            continue
        for canonical, spec in fields.items():
            aliases = spec.get("aliases") if isinstance(spec, dict) else None
            group = [canonical, *(aliases or [])]
            for term in group:
                n = _norm(term)
                if not n:
                    continue
                bucket = idx.setdefault(n, [])
                for g in group:
                    if g not in bucket:
                        bucket.append(g)

    # One ontology is live at a time; keep the cache from growing unbounded.
    _ALIAS_CACHE.clear()
    _ALIAS_CACHE[cache_key] = idx
    return idx


def _expand_keys(col: str, alias_index: dict[str, list[str]]) -> list[str]:
    """``col`` plus every ontology-equivalent label, ``col`` first."""
    out = [col]
    for term in alias_index.get(_norm(col), []):
        if term not in out:
            out.append(term)
    return out


def _load() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(mapping, ontology)``.

    Both are read **DB-first** (``agent_tools.SopFieldMapping`` /
    ``ClaimOntologyField``) and fall back to the repo-root YAMLs when the
    respective table is empty/unavailable (keeps the standalone CLI working).
    Cached in-process and keyed by a cheap (count, max-updated) watermark so
    edits made in another process become visible here without a restart.
    """
    file_mapping, file_ontology = _load_files()
    wm = _db_watermark()
    if wm is None:
        return file_mapping, file_ontology  # no Django/tables → YAML fallback

    if _DB_CACHE.get("wm") != wm:
        _DB_CACHE["wm"] = wm
        _DB_CACHE["mapping"] = _mapping_from_db() if wm[0] > 0 else None
        _DB_CACHE["ontology"] = _ontology_from_db() if wm[2] > 0 else None

    mapping = _DB_CACHE.get("mapping") or file_mapping
    ontology = _DB_CACHE.get("ontology") or file_ontology
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
    mapping, ontology = _load()
    mappings = (mapping or {}).get("mappings") or {}
    if not mappings:
        return {}

    # Ontology aliases let a mapping's source key resolve even when the claim
    # image uses a different raw label for the same box (e.g. "PROVIDER NPI"
    # instead of "11 NPI"). This is how the ontology participates at runtime.
    alias_index = _alias_index_for(ontology)

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
                for key in _expand_keys(col, alias_index):
                    for space in search_spaces:
                        found = _deep_find(space, key)
                        if found is not None:
                            # Report the canonical mapping key, plus the alias
                            # actually matched when it differs (audit clarity).
                            via = f" via {key}" if key != col else ""
                            value, where = found, f"{system}:{col}{via}"
                            break
                    if value is not None:
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
