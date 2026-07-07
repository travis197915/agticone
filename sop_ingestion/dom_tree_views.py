"""
DOM-tree endpoint for the SOP HTML mirror
=========================================

``GET /api/ingest/sops/<sop_id>/dom-tree/``
    Returns the **nested HTML DOM tree** for an ingested SOP, so the UI can
    traverse the SOP exactly the way it'd read the source HTML page —
    every ``<section>`` / ``<h2>`` / ``<h3>`` / ``<ul>`` / ``<li>`` /
    ``<table>`` / ``<a href>`` becomes a node with a ``children`` array.

    Two read paths controlled by ``?source=``:

    * ``?source=neo4j`` (default) — read from the HtmlBlock subgraph the
      ingestion pipeline wrote into Neo4j (see
      ``uhc_sop_ingestion.agents.a10_write_neo4j.html_dom_writer``). Also
      attaches any semantic rules linked by ``:DERIVED_FROM`` to each
      block, so the UI can render the rule chip inline.
    * ``?source=live`` — bypass Neo4j; build the tree on the fly from the
      SOP's source HTML (or synthesise one for DOCX/PDF/XLSX). Useful
      when Neo4j hasn't been re-ingested yet, or for SOPs created before
      the DOM-mirror writer existed.

Response shape::

    {
      "sop_id":        76,
      "title":         "OBH Facets Timely Filing",
      "neo4j_sop_id":  "OBH_Facets_Timely_Filing:abc123",
      "source":        "neo4j" | "live_html" | "synthesised",
      "block_count":   402,
      "root_count":    14,
      "roots": [
        {
          "block_id": "html-abc",
          "tag": "section",
          "kind": "section",
          "sub_label": "HtmlSection",
          "section_id": "introduction",
          "label": "Introduction",
          "text": "...",
          "html_snippet": "<section>...",
          "depth": 0,
          "order": 0,
          "is_root": true,
          "derived_rules": [],
          "children": [ ... ]
        }
      ]
    }
"""
from __future__ import annotations

import base64
import logging
import os
from collections import defaultdict
from typing import Any

from django.shortcuts import get_object_or_404
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AuditSop

log = logging.getLogger(__name__)


# ── Neo4j driver (module-singleton, lazy) ───────────────────────────────────


_DRIVER = None


def _neo4j_driver():
    """Return a cached driver instance, or None if Neo4j is not configured."""
    global _DRIVER
    if _DRIVER is not None:
        return _DRIVER
    host = os.environ.get("NEO4J_HOST")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    if not host or not password:
        return None
    try:
        from neo4j import GraphDatabase

        from sop_backend.db_config import neo4j_uri_from_env
    except ImportError:  # pragma: no cover — driver is in requirements
        return None
    try:
        _DRIVER = GraphDatabase.driver(
            neo4j_uri_from_env(),
            auth=(user, password),
            connection_timeout=5,
        )
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("dom_tree_views: neo4j connect failed: %s", exc)
        return None
    return _DRIVER


def _neo4j_database() -> str:
    return os.environ.get("NEO4J_DATABASE", "neo4j")


# ── Read from Neo4j ─────────────────────────────────────────────────────────


_BLOCK_FIELDS = (
    "block_id tag kind depth tree_depth order section_id label text "
    "html_snippet is_root parent_block_id href fragment external_url "
    "target_block_id unresolved sub_label"
).split()


def _load_dom_from_neo4j(neo4j_sop_id: str, *, with_rules: bool) -> dict | None:
    """Walk the HtmlBlock subgraph for ``neo4j_sop_id`` and return roots+flat.

    Returns ``None`` when the driver is unavailable; an empty dict when the
    SOP simply has no blocks written yet.
    """
    drv = _neo4j_driver()
    if drv is None:
        return None
    db = _neo4j_database()

    try:
        with drv.session(database=db) as s:
            # 1) All HtmlBlock nodes for this SOP, plus their sub-label.
            blocks_q = """
                MATCH (b:HtmlBlock {sop_id:$sop_id})
                RETURN b, labels(b) AS labels
            """
            block_rows = list(s.run(blocks_q, {"sop_id": neo4j_sop_id}))

            # 2) The semantic GraphNodes that resolve back to each block.
            derived: dict[str, list[dict]] = defaultdict(list)
            if with_rules:
                derived_q = """
                    MATCH (g:GraphNode {sop_id:$sop_id})-[:DERIVED_FROM]->(b:HtmlBlock {sop_id:$sop_id})
                    RETURN b.block_id AS block_id,
                           g.node_type AS node_type,
                           g.node_key  AS node_key,
                           g.label     AS label,
                           g.display_order AS display_order,
                           coalesce(g.decision_type, "")   AS decision_type,
                           coalesce(g.is_exception, false) AS is_exception
                """
                for row in s.run(derived_q, {"sop_id": neo4j_sop_id}):
                    derived[row["block_id"]].append({
                        "node_key":      row["node_key"],
                        "node_type":     row["node_type"],
                        "label":         row["label"],
                        "display_order": row["display_order"],
                        "decision_type": row["decision_type"],
                        "is_exception":  row["is_exception"],
                    })
    except Exception as exc:
        log.warning("dom_tree_views: neo4j read failed for %s: %s",
                    neo4j_sop_id, exc)
        return None

    if not block_rows:
        return {"roots": [], "flat": [], "source": "neo4j"}

    # Convert Neo4j Node objects → plain dicts.
    flat: list[dict] = []
    for row in block_rows:
        node = row["b"]
        # Drop the marker label, keep the type-specific one (HtmlSection, ...).
        sublabels = [lab for lab in row["labels"] if lab != "HtmlBlock"]
        sub_label = sublabels[0] if sublabels else (node.get("sub_label") or "HtmlBlock")
        rec: dict[str, Any] = {f: node.get(f) for f in _BLOCK_FIELDS}
        rec["sub_label"] = sub_label
        rec["block_id"] = node.get("block_id")
        rec["derived_rules"] = derived.get(rec["block_id"], [])
        rec["children"] = []
        flat.append(rec)

    # Build the parent→children tree.
    by_id = {b["block_id"]: b for b in flat}
    roots: list[dict] = []
    for b in flat:
        pid = b.get("parent_block_id") or ""
        if not pid:
            if b.get("is_root"):
                roots.append(b)
            continue
        parent = by_id.get(pid)
        if parent is not None:
            parent["children"].append(b)
        else:
            # Orphan (parent not in result) — treat as root.
            roots.append(b)

    # Stable order: sort siblings by their stored `order` field.
    def _sort(blocks: list[dict]) -> None:
        blocks.sort(key=lambda x: (x.get("order") or 0))
        for b in blocks:
            _sort(b["children"])

    _sort(roots)

    return {"roots": roots, "flat": flat, "source": "neo4j"}


# ── Fallback: build the tree live from the source HTML ─────────────────────


def _live_state_for(sop: AuditSop) -> dict | None:
    """Return a minimal pipeline-state dict for ``html_dom.build_dom_tree``.

    Re-fetches the SOP's source HTML (with a short timeout). Returns None
    when the source isn't reachable / isn't HTML.
    """
    fmt = (sop.doc_format or "HTML").upper()
    if fmt != "HTML":
        # html_dom synthesises a tree from parsed state for non-HTML, but the
        # parsed state isn't available here — we'd need to re-run the parser.
        # Cheaper to just return the AuditSop's structured fields as a tree.
        return _synth_state_from_audit_sop(sop)
    url = (sop.url or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        import requests
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        body = r.content or r.text.encode("utf-8", "replace")
    except Exception as exc:
        log.info("dom_tree_views: fetch failed for %s: %s", url, exc)
        return None
    return {
        "raw_bytes_b64": base64.b64encode(body).decode("ascii"),
        "doc_format":    "HTML",
        "current_url":   url,
        "metadata":      {"title": sop.title or ""},
    }


def _synth_state_from_audit_sop(sop: AuditSop) -> dict:
    """Build a pipeline-state-shaped dict from the AuditSop ORM rows so
    ``html_dom._synthesise_from_state`` can produce a tree for non-HTML SOPs."""
    pre_sections = [
        {
            "name":  pc.label or pc.category,
            "items": [{"text": t} for t in (pc.content_text or "").split("\n")
                      if t.strip()],
        }
        for pc in sop.preconditions.all().order_by("display_order", "id")
    ]
    steps = [
        {
            "number":     step.step_number,
            "question":   step.question or "",
            "intro_text": step.intro_text or "",
            "decision_rows": [
                {
                    "condition_if":  d.condition_if or "",
                    "action":        d.action_text or d.action_summary or "",
                    "decision_type": d.decision_type or "",
                }
                for d in step.decisions.all().order_by("row_index")
            ],
        }
        for step in sop.steps.all().order_by("step_number")
    ]
    return {
        "doc_format":   sop.doc_format or "DOCX",
        "metadata":     {"title": sop.title or ""},
        "pre_sections": pre_sections,
        "steps":        steps,
    }


def _build_tree_live(sop: AuditSop) -> dict:
    """Re-build the DOM tree without consulting Neo4j."""
    state = _live_state_for(sop) or _synth_state_from_audit_sop(sop)
    try:
        from uhc_sop_ingestion.html_dom import build_dom_tree
    except ImportError:
        log.warning("dom_tree_views: uhc_sop_ingestion.html_dom not importable")
        return {"roots": [], "flat": [], "source": "unavailable"}
    tree = build_dom_tree(state)
    # `derived_rules` is a Neo4j-only concept; leave it empty in live mode.
    for b in tree.get("flat", []):
        b.setdefault("derived_rules", [])
    tag = "live_html" if tree.get("source") == "html" else "synthesised"
    tree["source"] = tag
    return tree


# ── DRF view ────────────────────────────────────────────────────────────────


class SopDomTreeView(APIView):
    """``GET /api/ingest/sops/<sop_id>/dom-tree/``"""

    permission_classes = [AllowAny]

    def get(self, request: Request, sop_id: int) -> Response:
        sop: AuditSop = get_object_or_404(AuditSop, pk=sop_id)

        source = (request.query_params.get("source") or "neo4j").lower()
        with_rules = (request.query_params.get("with_rules", "1")
                      .lower() in ("1", "true", "yes"))

        tree: dict | None = None
        if source == "neo4j" and sop.neo4j_sop_id:
            tree = _load_dom_from_neo4j(sop.neo4j_sop_id, with_rules=with_rules)
            if not (tree and tree.get("flat")):
                # Neo4j path empty — fall through to live so the UI never
                # gets an empty tree just because the SOP hasn't been
                # re-ingested since the html_dom_writer landed.
                tree = None

        if tree is None or source == "live":
            tree = _build_tree_live(sop)

        flat = tree.get("flat") or []
        return Response({
            "sop_id":       sop.id,
            "title":        sop.title or "",
            "neo4j_sop_id": sop.neo4j_sop_id or "",
            "source":       tree.get("source") or "unavailable",
            "block_count":  len(flat),
            "root_count":   sum(1 for b in flat if b.get("is_root")),
            "roots":        tree.get("roots") or [],
        })


# ── Convenience: lookup a single block + its semantic rules ─────────────────


class SopDomBlockView(APIView):
    """``GET /api/ingest/sops/<sop_id>/dom-tree/<block_id>/``

    Returns one block (no children traversal) plus its derived semantic
    rules. Useful for the SPA when the user clicks a node and wants the
    detail panel without re-walking the whole tree.
    """

    permission_classes = [AllowAny]

    def get(self, request: Request, sop_id: int, block_id: str) -> Response:
        sop: AuditSop = get_object_or_404(AuditSop, pk=sop_id)
        if not sop.neo4j_sop_id:
            return Response(
                {"error": "SOP has no Neo4j subgraph (not yet ingested)."},
                status=404,
            )
        drv = _neo4j_driver()
        if drv is None:
            return Response({"error": "Neo4j unavailable."}, status=503)
        try:
            with drv.session(database=_neo4j_database()) as s:
                row = s.run(
                    """
                    MATCH (b:HtmlBlock {sop_id:$sop_id, block_id:$bid})
                    OPTIONAL MATCH (g:GraphNode {sop_id:$sop_id})-[:DERIVED_FROM]->(b)
                    RETURN b, labels(b) AS labels,
                           collect({
                             node_key:      g.node_key,
                             node_type:     g.node_type,
                             label:         g.label,
                             decision_type: coalesce(g.decision_type, ""),
                             is_exception:  coalesce(g.is_exception, false),
                             display_order: g.display_order
                           }) AS rules
                    """,
                    {"sop_id": sop.neo4j_sop_id, "bid": block_id},
                ).single()
        except Exception as exc:
            log.warning("dom_tree_views: neo4j block lookup failed: %s", exc)
            return Response({"error": "Neo4j read failed."}, status=502)
        if row is None:
            return Response({"error": "block not found."}, status=404)
        node = row["b"]
        sublabels = [lab for lab in row["labels"] if lab != "HtmlBlock"]
        rec = {f: node.get(f) for f in _BLOCK_FIELDS}
        rec["sub_label"] = sublabels[0] if sublabels else node.get("sub_label")
        # Filter out the empty {} row collect() returns when no DERIVED_FROM matched.
        rec["derived_rules"] = [
            r for r in row["rules"]
            if r and r.get("node_key")
        ]
        return Response(rec)
