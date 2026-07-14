"""
SOP Exclusion REST endpoints
============================

Per-SOP user-curated exclusion list — what the UI calls when a user clicks
"mark this rule/step/section as excluded for this SOP".

Routes (mounted under ``/api/ingest/`` from sop_ingestion.urls)
---------------------------------------------------------------
GET    /api/ingest/sops/<int:sop_id>/exclusions/
       List every exclusion attached to the SOP.

POST   /api/ingest/sops/<int:sop_id>/exclusions/
       Create OR update (upsert by target_kind+target_key) an exclusion.

DELETE /api/ingest/sops/<int:sop_id>/exclusions/<int:exclusion_id>/
       Remove one exclusion.

POST   /api/ingest/sops/<int:sop_id>/exclusions/toggle/
       Convenience: idempotent "make sure this thing is excluded" / "make
       sure this thing is NOT excluded" given just a target_kind+target_key.

Payload (POST / toggle)
-----------------------
::

    {
      "target_kind": "rule" | "step" | "section" | "sop" | "graph_node",
      "target_key":  "<stable key>",       # e.g. "step:22:4:0"
      "label":       "optional label",
      "reason":      "optional auditor note",
      "snippet_text": "optional captured source text",
      "metadata":    { ...free-form... }
    }

Stable target keys (must match keys emitted by
``/api/builder/workflows/<id>/attachable/``):

* ``rule``       → ``"pre:<sop_id>:<precondition_id>:<rule_idx>"`` or
                   ``"step:<sop_id>:<step_no>:<row_idx>"``
* ``step``       → ``"step:<sop_id>:<step_no>"``
* ``section``    → ``"pre:<sop_id>:<precondition_id>"``
* ``sop``        → ``"sop:<sop_id>"``
* ``graph_node`` → raw ``AuditGraphNode.node_key`` (e.g. ``"step_4_d0"``)
"""

from __future__ import annotations

import re

from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    AuditDecision,
    AuditGraphNode,
    AuditPrecondition,
    AuditSop,
    AuditStep,
    SopExclusion,
)
from .html_blocks import (
    extract_blocks,
    fetch_sanitized_html,
    fetch_html_or_fallback,
    get_block_by_id,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


_VALID_KINDS = {k for k, _ in SopExclusion.TARGET_KINDS}


def _validate_target(sop: AuditSop, kind: str, key: str) -> tuple[bool, str]:
    """Sanity-check that target_kind+target_key actually references something
    in this SOP. Returns (ok, error_message).

    Done in Python (not in the LLM) so the UI gets immediate feedback when
    it submits a stale or wrong-SOP key.
    """
    if kind not in _VALID_KINDS:
        return False, f"target_kind must be one of {sorted(_VALID_KINDS)}"
    if not key:
        return False, "target_key is required"

    if kind == "sop":
        m = re.match(r"^sop:(\d+)$", key)
        if not m or int(m.group(1)) != sop.id:
            return False, f"target_key must be 'sop:{sop.id}'"
        return True, ""

    if kind == "rule":
        m = re.match(r"^(pre|step):(\d+):(\d+)(?::(\d+))?$", key)
        if not m:
            return (
                False,
                "rule target_key must be 'pre:<sop>:<pc>:<idx>' or 'step:<sop>:<step>:<row>'",
            )
        if int(m.group(2)) != sop.id:
            return False, f"rule target_key sop_id mismatch (expected {sop.id})"
        return True, ""

    if kind == "step":
        m = re.match(r"^step:(\d+):(\d+)$", key)
        if not m:
            return False, "step target_key must be 'step:<sop>:<step_no>'"
        if int(m.group(1)) != sop.id:
            return False, f"step target_key sop_id mismatch (expected {sop.id})"
        if not AuditStep.objects.filter(sop=sop, step_number=int(m.group(2))).exists():
            return False, f"step {m.group(2)} not found in sop {sop.id}"
        return True, ""

    if kind == "section":
        m = re.match(r"^pre:(\d+):(\d+)$", key)
        if not m:
            return False, "section target_key must be 'pre:<sop>:<precondition_id>'"
        if int(m.group(1)) != sop.id:
            return False, f"section target_key sop_id mismatch (expected {sop.id})"
        if not AuditPrecondition.objects.filter(sop=sop, id=int(m.group(2))).exists():
            return False, f"precondition {m.group(2)} not found in sop {sop.id}"
        return True, ""

    if kind == "graph_node":
        if not AuditGraphNode.objects.filter(sop=sop, node_key=key).exists():
            return False, f"graph node '{key}' not found in sop {sop.id}"
        return True, ""

    if kind == "html_block":
        m = re.match(r"^html:(\d+):(html-[A-Za-z0-9]+)$", key)
        if not m:
            return False, "html_block target_key must be 'html:<sop>:html-<id>'"
        if int(m.group(1)) != sop.id:
            return False, f"html_block target_key sop_id mismatch (expected {sop.id})"
        # We don't require the block to exist in the current extraction —
        # the source HTML may have shifted. The frontend always passes
        # snippet_text so the exclusion remains self-contained.
        return True, ""

    return False, "unsupported target_kind"


def _serialize(ex: SopExclusion) -> dict:
    return {
        "id": ex.id,
        "sop_id": ex.sop_id,
        "target_kind": ex.target_kind,
        "target_key": ex.target_key,
        "label": ex.label,
        "reason": ex.reason,
        "snippet_text": ex.snippet_text,
        "metadata": ex.metadata or {},
        "created_by_id": ex.created_by_id,
        "created_by_email": ex.created_by_email,
        "created_at": ex.created_at.isoformat() if ex.created_at else None,
        "updated_at": ex.updated_at.isoformat() if ex.updated_at else None,
    }


def _autoderive_label_and_snippet(
    sop: AuditSop, kind: str, key: str
) -> tuple[str, str]:
    """Best-effort fill of label + snippet_text from the audit tables when
    the caller doesn't pass them — so the UI never has to repeat data it
    already has from /attachable/. Deterministic; no LLM.
    """
    try:
        if kind == "rule":
            m = re.match(r"^(pre|step):(\d+):(\d+):(\d+)$", key)
            if not m:
                return "", ""
            src, _sop_id, a, b = (
                m.group(1),
                m.group(2),
                int(m.group(3)),
                int(m.group(4)),
            )
            if src == "pre":
                pc = AuditPrecondition.objects.filter(sop=sop, id=a).first()
                if not pc or not (pc.llm_rules or []):
                    return "", ""
                if b >= len(pc.llm_rules):
                    return "", ""
                r = pc.llm_rules[b] or {}
                label = (r.get("condition") or r.get("action") or pc.label or "")[:255]
                snippet = pc.content_text or ""
                return label, snippet
            else:  # decision
                step = AuditStep.objects.filter(sop=sop, step_number=a).first()
                d = (
                    AuditDecision.objects.filter(step=step, row_index=b).first()
                    if step
                    else None
                )
                if not d:
                    return "", ""
                label = (d.condition_if or d.action_text or d.action_summary or "")[
                    :255
                ]
                snippet = (
                    (step.intro_text or "")
                    + "\n\n"
                    + (d.action_text or d.action_summary or "")
                ).strip()
                return label, snippet
        if kind == "step":
            m = re.match(r"^step:(\d+):(\d+)$", key)
            if not m:
                return "", ""
            step = AuditStep.objects.filter(
                sop=sop, step_number=int(m.group(2))
            ).first()
            if not step:
                return "", ""
            label = (
                f"Step {step.step_number}"
                + (f": {step.question}" if step.question else "")
            )[:255]
            return label, step.intro_text or step.narrative_context or ""
        if kind == "section":
            m = re.match(r"^pre:(\d+):(\d+)$", key)
            if not m:
                return "", ""
            pc = AuditPrecondition.objects.filter(sop=sop, id=int(m.group(2))).first()
            if not pc:
                return "", ""
            return (pc.label or pc.category)[:255], pc.content_text or ""
        if kind == "sop":
            return (sop.title or "Whole SOP")[:255], (
                sop.purpose or sop.llm_summary or ""
            )
        if kind == "graph_node":
            n = AuditGraphNode.objects.filter(sop=sop, node_key=key).first()
            if not n:
                return "", ""
            return (n.label or n.node_key)[:255], ((n.details or {}).get("text") or "")
        if kind == "html_block":
            m = re.match(r"^html:\d+:(html-[A-Za-z0-9]+)$", key)
            if not m:
                return "", ""
            blk = get_block_by_id(sop, m.group(1))
            if not blk:
                return "", ""
            return blk["label"][:255], blk["text"]
    except Exception:
        return "", ""
    return "", ""


# ── Views ───────────────────────────────────────────────────────────────────


class SopHtmlBlocksView(APIView):
    """GET the list of selectable HTML sections for a SOP.

    The list is **deterministic** — each block carries a content-hashed
    ``block_id`` so the same source HTML always yields the same id, which
    means a `SopExclusion` row created against a block survives
    re-ingestion as long as the underlying text doesn't change.

    Response::

        {
          "sop_id": 22,
          "source_url": "...",
          "doc_format": "HTML",
          "count": 81,
          "blocks": [
            {
              "block_id": "html-abc123def",
              "kind":     "heading|table|list|paragraph|callout|...",
              "tag":      "h2|table|ul|p|blockquote",
              "label":    "Pre-Service Authorization",
              "html":     "<h2>...</h2>",
              "text":     "...",
              "depth":    2,
              "order":    17,
              "is_excluded": true,         # already in SopExclusion
              "exclusion_id": 42           # row id when is_excluded
            }, ...
          ]
        }
    """

    def get(self, _request: Request, sop_id: int) -> Response:
        sop = get_object_or_404(AuditSop, pk=sop_id)
        blocks = extract_blocks(sop)
        # Mark blocks already excluded so the UI can render their state.
        existing = {
            ex.target_key: ex.id
            for ex in sop.user_exclusions.filter(target_kind="html_block")
        }
        enriched = []
        for b in blocks:
            tgt = f"html:{sop.id}:{b['block_id']}"
            enriched.append(
                {
                    **b,
                    "target_kind": "html_block",
                    "target_key": tgt,
                    "is_excluded": tgt in existing,
                    "exclusion_id": existing.get(tgt),
                }
            )
        return Response(
            {
                "sop_id": sop.id,
                "source_url": sop.url or "",
                "doc_format": sop.doc_format or "HTML",
                "count": len(enriched),
                "blocks": enriched,
            }
        )


class SopSourceHtmlView(APIView):
    """GET the sanitized **source HTML** of the SOP for click-to-pick mode.

    The SPA mounts the returned body fragment via ``dangerouslySetInnerHTML``
    inside a contained scroller, attaches its own click/hover handlers to
    every block-level child, and toggles ``html_block`` exclusions against
    ``/exclusions/toggle/`` directly — no per-section list, no extraction.

    Response::

        {
          "sop_id": 22,
          "doc_format": "HTML",
          "source_url": "...",
          "available": true,
          "html": "<div>...</div>...",   # body innerHTML, sanitized
          "reason": ""                   # populated when available=false
        }

    For DOCX / PDF / XLSX uploads (or when the source URL can't be reached)
    ``available=false`` and the SPA should fall back to the extracted
    blocks list returned by ``/html-blocks/``.
    """

    def get(self, _request: Request, sop_id: int) -> Response:
        sop = get_object_or_404(AuditSop, pk=sop_id)
        html, reason, is_fallback = fetch_html_or_fallback(sop)
        # Existing user-marked html_block exclusions (target_keys) so the
        # SPA can outline them on load without re-querying /attachable/.
        excluded_keys = list(
            sop.user_exclusions.filter(target_kind="html_block").values_list(
                "target_key", flat=True
            )
        )
        return Response(
            {
                "sop_id": sop.id,
                "doc_format": sop.doc_format or "HTML",
                "source_url": sop.url or "",
                "available": bool(html),
                "html": html,
                "reason": reason,
                "is_fallback": is_fallback,
                "excluded_target_keys": excluded_keys,
            }
        )


class SopStoredHtmlView(APIView):
    """GET the crawled source SOP HTML cached in MongoDB.

    Serves the raw original SOP template (with the rules), crawled once with
    BeautifulSoup and stored in the ``sop_source_html`` collection. HTML SOPs
    are crawled lazily on first request when not yet cached (``?crawl=0`` to
    disable). PDF uploads and node/YAML SOPs are not viewable and return
    ``available=false``.

    Response::

        {"sop_id": 12, "available": true, "kind": "html",
         "html": "<html>…</html>", "source_url": "https://…",
         "crawled_at": "…", "reason": ""}
    """

    def get(self, request: Request, sop_id: int) -> Response:
        from .sop_html_crawler import (
            classify_sop_source,
            crawl_and_store,
            get_stored,
            is_crawlable_url,
        )

        sop = get_object_or_404(AuditSop, pk=sop_id)
        kind = classify_sop_source(sop.url)

        # A manually loaded HTML template (e.g. for a file:// upload) always
        # wins: if it is already stored in Mongo, serve it regardless of the
        # source URL scheme so the UI can show the original SOP template.
        stored = get_stored(sop.id)
        if stored and stored.get("html"):
            return Response(
                {
                    "sop_id": sop.id,
                    "available": True,
                    "kind": "html",
                    "html": stored["html"],
                    "source_url": sop.url or "",
                    "crawled_at": stored.get("crawled_at"),
                    "reason": "",
                }
            )

        if not is_crawlable_url(sop.url):
            reason = (
                "This SOP is a PDF upload with no HTML source."
                if kind == "pdf"
                else "This SOP is node/workflow-based (no source document)."
            )
            return Response(
                {
                    "sop_id": sop.id,
                    "available": False,
                    "kind": kind,
                    "html": "",
                    "source_url": sop.url or "",
                    "reason": reason,
                }
            )

        doc = get_stored(sop.id)
        if not doc and (request.query_params.get("crawl") or "1") != "0":
            try:
                doc = crawl_and_store(sop)
            except Exception as exc:  # noqa: BLE001
                return Response(
                    {
                        "sop_id": sop.id,
                        "available": False,
                        "kind": kind,
                        "html": "",
                        "source_url": sop.url or "",
                        "reason": f"Crawl failed: {exc}",
                    }
                )

        if not doc or not doc.get("html"):
            return Response(
                {
                    "sop_id": sop.id,
                    "available": False,
                    "kind": kind,
                    "html": "",
                    "source_url": sop.url or "",
                    "reason": "SOP HTML not available yet.",
                }
            )

        return Response(
            {
                "sop_id": sop.id,
                "available": True,
                "kind": "html",
                "html": doc["html"],
                "source_url": sop.url or "",
                "crawled_at": doc.get("crawled_at"),
                "reason": "",
            }
        )


class SopExclusionListCreateView(APIView):
    """GET / POST exclusions for a single SOP."""

    def get(self, _request: Request, sop_id: int) -> Response:
        sop = get_object_or_404(AuditSop, pk=sop_id)
        items = [
            _serialize(ex) for ex in sop.user_exclusions.all().order_by("-updated_at")
        ]
        return Response({"sop_id": sop.id, "count": len(items), "results": items})

    def post(self, request: Request, sop_id: int) -> Response:
        sop = get_object_or_404(AuditSop, pk=sop_id)
        data = request.data if isinstance(request.data, dict) else {}
        kind = (data.get("target_kind") or "rule").strip()
        key = (data.get("target_key") or "").strip()

        ok, err = _validate_target(sop, kind, key)
        if not ok:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)

        # Auto-fill label/snippet when the caller didn't provide them.
        label_in = (data.get("label") or "").strip()
        snippet_in = (data.get("snippet_text") or "").strip()
        if not label_in or not snippet_in:
            d_label, d_snippet = _autoderive_label_and_snippet(sop, kind, key)
            if not label_in:
                label_in = d_label
            if not snippet_in:
                snippet_in = d_snippet

        defaults = dict(
            label=label_in[:255],
            reason=(data.get("reason") or "").strip(),
            snippet_text=snippet_in,
            metadata=data.get("metadata") or {},
            created_by_id=str(getattr(request.user, "id", "") or "")[:64],
            created_by_email=getattr(request.user, "email", "") or "",
        )
        try:
            ex, created = SopExclusion.objects.update_or_create(
                sop=sop,
                target_kind=kind,
                target_key=key,
                defaults=defaults,
            )
        except IntegrityError as e:
            return Response({"error": str(e)}, status=status.HTTP_409_CONFLICT)

        return Response(
            _serialize(ex),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SopExclusionDetailView(APIView):
    """DELETE one exclusion by id."""

    def delete(self, _request: Request, sop_id: int, exclusion_id: int) -> Response:
        sop = get_object_or_404(AuditSop, pk=sop_id)
        ex = get_object_or_404(SopExclusion, pk=exclusion_id, sop=sop)
        ex.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SopExclusionToggleView(APIView):
    """POST a (target_kind, target_key, on?) triple — idempotent on/off.

    Body
    ----
    ``{ "target_kind": "...", "target_key": "...", "on": true | false }``

    When ``on`` is omitted, the endpoint flips current state.
    """

    def post(self, request: Request, sop_id: int) -> Response:
        sop = get_object_or_404(AuditSop, pk=sop_id)
        data = request.data if isinstance(request.data, dict) else {}
        kind = (data.get("target_kind") or "rule").strip()
        key = (data.get("target_key") or "").strip()
        on = data.get("on")  # None → toggle

        ok, err = _validate_target(sop, kind, key)
        if not ok:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)

        existing = SopExclusion.objects.filter(
            sop=sop,
            target_kind=kind,
            target_key=key,
        ).first()
        if on is None:
            on = existing is None

        if not on:
            if existing:
                existing.delete()
            return Response(
                {
                    "sop_id": sop.id,
                    "target_kind": kind,
                    "target_key": key,
                    "excluded": False,
                }
            )

        # on == True → upsert with auto-derived label/snippet
        d_label, d_snippet = _autoderive_label_and_snippet(sop, kind, key)
        defaults = dict(
            label=((data.get("label") or d_label) or "")[:255],
            reason=(data.get("reason") or "").strip(),
            snippet_text=(data.get("snippet_text") or d_snippet) or "",
            metadata=data.get("metadata") or {},
            created_by_id=str(getattr(request.user, "id", "") or "")[:64],
            created_by_email=getattr(request.user, "email", "") or "",
        )
        ex, _created = SopExclusion.objects.update_or_create(
            sop=sop,
            target_kind=kind,
            target_key=key,
            defaults=defaults,
        )
        return Response(
            {
                "sop_id": sop.id,
                "target_kind": kind,
                "target_key": key,
                "excluded": True,
                "exclusion": _serialize(ex),
            }
        )
