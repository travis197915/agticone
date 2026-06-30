"""
HTML-section extractor for SOP exclusion picker
================================================

Given an :class:`AuditSop` row, produce a flat list of "blocks" the user can
mark as excluded from the UI. The list is **deterministic**, **stable across
re-fetches** (block_id is content-hashed) and works for every supported source
format:

* ``HTML``                 → fetch ``sop.url`` (with caching), parse with
                             BeautifulSoup, walk the DOM picking sectioning
                             tags (headings, tables, lists, paragraphs,
                             blockquotes, callouts).
* ``DOCX / PDF / XLSX``    → derive blocks from the parsed audit tables
                             (preconditions, steps, annotations, codes,
                             group_limits, references) — those rows ARE the
                             extracted sections for non-HTML sources.

The returned shape is:

    {
        "block_id": "html-abc123…",  # sha1 of the block's html
        "kind":     "heading|table|list|paragraph|callout|metadata",
        "tag":      "h2|table|ul|p|blockquote|...",   # source tag (html only)
        "label":    "Pre-Service Authorization",      # human label
        "html":     "<h2>...</h2>",                   # rendered html
        "text":     "...",                            # plaintext
        "depth":    2,                                # heading depth (0 if N/A)
        "order":    17,                               # document order
    }

No LLM is involved.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Iterable

from django.core.cache import cache

from .models import AuditSop

log = logging.getLogger(__name__)


# ── Public API ──────────────────────────────────────────────────────────────


def extract_blocks(sop: AuditSop) -> list[dict]:
    """Return the canonical list of selectable HTML blocks for ``sop``.

    Cached for 5 min per (sop_id, url, content_hash) so repeated picker
    opens don't refetch.
    """
    cache_key = f"sopxhtml:{sop.id}:{sop.content_hash or 'nohash'}"
    hit = cache.get(cache_key)
    if hit is not None:
        return hit

    fmt = (sop.doc_format or "HTML").upper()
    blocks: list[dict]
    if fmt == "HTML":
        blocks = _extract_from_html(sop) or _extract_from_audit_tables(sop)
    else:
        blocks = _extract_from_audit_tables(sop)

    cache.set(cache_key, blocks, timeout=300)
    return blocks


def block_id_for_html(html: str) -> str:
    """Stable, content-addressed id used as ``block_id`` and embedded in
    the exclusion's ``target_key`` (``html:<sop>:<block_id>``).

    Matched on the client by ``sha1(text).slice(0,12)`` over the same UTF-8
    bytes — Web Crypto's ``crypto.subtle.digest('SHA-1', ...)`` produces an
    identical digest when the input string is identical, so the SPA can mark
    a click-picked element as already-excluded by hashing its ``outerHTML``.
    """
    h = hashlib.sha1((html or "").encode("utf-8", "replace")).hexdigest()
    return f"html-{h[:12]}"


def fetch_sanitized_html(sop: AuditSop) -> tuple[str | None, str]:
    """Return ``(html, reason)`` where ``html`` is the sanitized **body
    innerHTML** of the SOP's source document, safe to mount in the SPA via
    ``dangerouslySetInnerHTML``, and ``reason`` is a short string explaining
    why the html is None (empty when available).

    Sanitisation removes ``<script>``, ``<style>``, ``<link>``, ``<form>``,
    ``<iframe>``, ``<object>``, ``<embed>``; strips ``on*=`` handlers and
    ``javascript:`` URLs; drops ``<meta>`` / ``<base>``. The structural DOM
    is left intact so per-element hashing matches the extractor."""
    raw = _fetch_html(sop)
    if not raw:
        return None, "Source HTML unavailable (non-HTTP source or fetch failed)"
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None, "BeautifulSoup not installed"
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all([
        "script", "style", "link", "form", "iframe", "object",
        "embed", "meta", "base", "noscript",
    ]):
        tag.decompose()
    # Strip dangerous attributes
    for el in soup.find_all(True):
        attrs = dict(el.attrs)
        for k, v in attrs.items():
            kl = k.lower()
            if kl.startswith("on"):
                del el.attrs[k]
                continue
            if kl in {"href", "src", "action", "formaction"} and isinstance(v, str):
                if v.strip().lower().startswith(("javascript:", "data:")):
                    el.attrs[k] = "#"
    body = soup.body or soup
    html = body.decode_contents() if hasattr(body, "decode_contents") else str(body)
    return html, ""


def fetch_html_or_fallback(sop: AuditSop) -> tuple[str, str, bool]:
    """Return ``(html, reason, is_fallback)``.

    Tries to serve the live source HTML first. When that is unavailable (the
    SOP was ingested from DOCX/PDF/XLSX, the URL is localhost, or the remote
    host is down) it synthesises clean, readable HTML from the already-parsed
    audit tables that live in the database — so the right-hand panel of the
    fullscreen picker always has *something* to display.

    ``is_fallback`` is True when synthesised HTML is returned instead of the
    original source document.
    """
    html, reason = fetch_sanitized_html(sop)
    if html:
        return html, reason, False

    # Build HTML from the parsed audit tables.
    blocks = _extract_from_audit_tables(sop)
    if not blocks:
        return "", reason or "No content available for this SOP.", False

    title = _e(sop.title or f"SOP #{sop.id}")
    pieces: list[str] = [
        f'<div class="sop-synthesised-notice">'
        f'<strong>Note:</strong> Showing structured data extracted during ingestion from Graph DB.'
        f'</div>',
        f'<h1>{title}</h1>',
    ]
    for block in blocks:
        pieces.append(block["html"])
    return "\n".join(pieces), "", True


def get_block_by_id(sop: AuditSop, block_id: str) -> dict | None:
    """Convenience helper for the exclusion writer to auto-fill label/snippet."""
    for b in extract_blocks(sop):
        if b["block_id"] == block_id:
            return b
    return None


# ── HTML path (BeautifulSoup) ───────────────────────────────────────────────


_BLOCK_TAGS = {
    "h1": ("heading", 1), "h2": ("heading", 2), "h3": ("heading", 3),
    "h4": ("heading", 4), "h5": ("heading", 5), "h6": ("heading", 6),
    "table":      ("table",     0),
    "ul":         ("list",      0),
    "ol":         ("list",      0),
    "blockquote": ("callout",   0),
    "pre":        ("code",      0),
    "p":          ("paragraph", 0),
}
_SKIP_TAGS = {"script", "style", "noscript", "head", "meta", "link"}


def _storage_auth_headers(url: str) -> dict[str, str]:
    """Return ``X-Storage-Key`` / ``X-Storage-Secret`` headers when ``url``
    lives on the Toystack file-storage server, otherwise return ``{}``.

    This lets the Django backend fetch SOP HTML that was uploaded during
    ingestion without a separate proxy — the URL stored in ``sop.url`` is
    the storage retrieval URL and the auth headers are attached here.
    """
    import os
    storage_base = os.environ.get("STORAGE_URL", "").rstrip("/")
    if storage_base and url.startswith(storage_base):
        key = os.environ.get("STORAGE_ACCESS_KEY", "")
        secret = os.environ.get("STORAGE_SECRET", "")
        if key and secret:
            return {"X-Storage-Key": key, "X-Storage-Secret": secret}
    return {}


def _fetch_html(sop: AuditSop) -> str | None:
    """Fetch the raw HTML for an HTTP-served SOP. Returns None on failure.

    Automatically adds Toystack storage auth headers when the URL points to
    the configured ``STORAGE_URL`` (i.e. an HTML file uploaded during
    ingestion).
    """
    url = (sop.url or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    cache_key = f"sopxhtmlraw:{sop.id}:{sop.content_hash or 'nohash'}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        import requests
        headers = _storage_auth_headers(url)
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            log.info("html_blocks: %s returned %s", url, r.status_code)
            return None
        r.encoding = r.apparent_encoding or r.encoding
        text = r.text
        cache.set(cache_key, text, timeout=300)
        return text
    except Exception as exc:  # noqa: BLE001
        log.info("html_blocks: fetch failed for %s: %s", url, exc)
        return None


def _extract_from_html(sop: AuditSop) -> list[dict]:
    raw = _fetch_html(sop)
    if not raw:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    soup = BeautifulSoup(raw, "html.parser")
    for s in soup.find_all(_SKIP_TAGS):
        s.decompose()
    body = soup.body or soup
    blocks: list[dict] = []
    seen: set[str] = set()
    order = 0
    # We iterate in document order and only emit element-level tags listed
    # in _BLOCK_TAGS — that gives the auditor the right granularity without
    # exploding the list with inline spans.
    for el in body.descendants:
        if getattr(el, "name", None) is None:
            continue
        if el.name not in _BLOCK_TAGS:
            continue
        # Skip nested block tags whose container we already emitted (e.g. a
        # <p> inside a <blockquote>) to avoid duplicates.
        if _has_block_ancestor(el):
            continue
        html = str(el).strip()
        if not html:
            continue
        text = _normalize_ws(el.get_text(" ", strip=True))
        if not text:
            continue
        bid = block_id_for_html(html)
        if bid in seen:
            continue
        seen.add(bid)
        kind, depth = _BLOCK_TAGS[el.name]
        label = _label_for_block(kind, el, text)
        blocks.append({
            "block_id": bid,
            "kind":     kind,
            "tag":      el.name,
            "label":    label[:220],
            "html":     html,
            "text":     text[:4000],
            "depth":    depth,
            "order":    order,
        })
        order += 1
    return blocks


def _has_block_ancestor(el) -> bool:
    p = el.parent
    while p is not None and getattr(p, "name", None) is not None:
        if p.name in _BLOCK_TAGS and p.name != "p":
            # tables / lists / blockquotes ARE containers; their inner <p>
            # children should be skipped in favour of the container itself.
            return True
        p = p.parent
    return False


def _label_for_block(kind: str, el, text: str) -> str:
    if kind == "heading":
        return text or el.name.upper()
    if kind == "table":
        cap = el.find("caption")
        if cap and cap.get_text(strip=True):
            return f"Table — {cap.get_text(strip=True)}"
        first_row = el.find("tr")
        if first_row:
            cells = [c.get_text(" ", strip=True) for c in first_row.find_all(["th", "td"])]
            cells = [c for c in cells if c]
            if cells:
                return "Table — " + " | ".join(cells[:4])[:200]
        return "Table"
    if kind == "list":
        first = el.find("li")
        if first:
            head = first.get_text(" ", strip=True)
            return f"List · {head[:140]}"
        return "List"
    if kind == "callout":
        return f"Callout — {text[:140]}"
    if kind == "code":
        return f"Code — {text[:140]}"
    return text[:140]


_WS = re.compile(r"\s+")


def _normalize_ws(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


# ── Fallback path: derive blocks from the audit tables ──────────────────────


def _extract_from_audit_tables(sop: AuditSop) -> list[dict]:
    """Synthesize blocks for non-HTML sources (DOCX/PDF/XLSX) — or as a
    fallback when the source URL can't be fetched."""
    blocks: list[dict] = []
    order = 0

    def _push(kind: str, tag: str, label: str, html: str, text: str, depth: int = 0):
        nonlocal order
        if not html.strip():
            return
        blocks.append({
            "block_id": block_id_for_html(html),
            "kind":     kind,
            "tag":      tag,
            "label":    label[:220],
            "html":     html,
            "text":     text[:4000],
            "depth":    depth,
            "order":    order,
        })
        order += 1

    # SOP-level metadata as the first "section"
    summary = (sop.purpose or sop.llm_summary or sop.narrative_context or "").strip()
    if summary:
        _push(
            "metadata", "section",
            f"{sop.title or 'SOP'} — Overview",
            f"<section><h2>{_e(sop.title or 'SOP')}</h2><p>{_e(summary)}</p></section>",
            summary, depth=1,
        )

    # Pre-conditions
    for pc in sop.preconditions.all().order_by("display_order", "id"):
        text = (pc.content_text or "").strip()
        if not text:
            continue
        _push(
            "paragraph", "section",
            f"{pc.label or pc.category}",
            f"<section><h3>{_e(pc.label or pc.category)}</h3>"
            f"<p>{_e(text)}</p></section>",
            text, depth=2,
        )

    # Steps
    for step in sop.steps.all().order_by("step_number"):
        head = (f"Step {step.step_number}"
                + (f" — {step.question}" if step.question else "")).strip()
        body = (step.intro_text or step.narrative_context or "").strip()
        if not (head or body):
            continue
        _push(
            "paragraph", "section", head,
            f"<section><h3>{_e(head)}</h3>"
            + (f"<p>{_e(body)}</p>" if body else "")
            + "</section>",
            f"{head}\n\n{body}", depth=2,
        )
        # Decision rows as one table per step
        rows = list(step.decisions.all().order_by("row_index"))
        if rows:
            row_html = "".join(
                f"<tr><td>{_e(d.condition_if or '')}</td>"
                f"<td>{_e(d.action_text or d.action_summary or '')}</td>"
                f"<td>{_e(d.decision_type or '')}</td></tr>"
                for d in rows
            )
            tbl = (
                "<table><caption>Decisions for "
                f"{_e(head)}</caption>"
                "<thead><tr><th>If</th><th>Then</th><th>Type</th></tr></thead>"
                f"<tbody>{row_html}</tbody></table>"
            )
            _push("table", "table", f"Decision table — {head}", tbl,
                  "\n".join(
                      f"IF {d.condition_if} THEN {d.action_text or d.action_summary}"
                      for d in rows
                  ),
                  depth=3)

    # Annotations / callouts
    for ann in getattr(sop, "annotations", []).all() if hasattr(sop, "annotations") else []:
        text = (getattr(ann, "content_text", "") or "").strip()
        if not text:
            continue
        _push(
            "callout", "blockquote",
            f"Note · {text[:80]}",
            f"<blockquote>{_e(text)}</blockquote>",
            text, depth=2,
        )

    # Code tables
    codes = list(getattr(sop, "codes", []).all()) if hasattr(sop, "codes") else []
    if codes:
        rows = "".join(
            f"<tr><td>{_e(c.code_value)}</td>"
            f"<td>{_e(c.code_type)}</td>"
            f"<td>{_e(getattr(c, 'description', '') or '')}</td></tr>"
            for c in codes
        )
        tbl = (
            "<table><caption>Codes</caption>"
            "<thead><tr><th>Code</th><th>Type</th><th>Description</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
        _push("table", "table", "Codes table", tbl,
              "\n".join(f"{c.code_value} ({c.code_type})" for c in codes),
              depth=2)

    # Group limits
    for gl in getattr(sop, "group_limits", []).all() if hasattr(sop, "group_limits") else []:
        text = f"{gl.group_name} — INN {gl.inn_days} / OON {gl.oon_days}"
        _push("table", "table", text,
              f"<table><caption>{_e(gl.group_name)}</caption>"
              f"<tr><th>INN days</th><td>{_e(str(gl.inn_days))}</td></tr>"
              f"<tr><th>OON days</th><td>{_e(str(gl.oon_days))}</td></tr></table>",
              text, depth=3)

    # References
    for ref in getattr(sop, "references", []).all() if hasattr(sop, "references") else []:
        text = (getattr(ref, "ref_text", "") or "").strip()
        if not text:
            continue
        _push("callout", "blockquote",
              f"Reference · {text[:80]}",
              f"<blockquote>{_e(text)}</blockquote>",
              text, depth=2)

    # Raw-text paragraph fallback (catches anything the structured tables
    # missed, e.g. footnotes/legal blocks in DOCX). Cap to keep the list
    # sane — we only emit blocks longer than 40 chars that don't already
    # appear in the section list.
    seen_texts = {b["text"][:200] for b in blocks}
    raw = (sop.raw_text or "").strip()
    if raw:
        for para in re.split(r"\n{2,}", raw):
            t = _normalize_ws(para)
            if len(t) < 40 or t[:200] in seen_texts:
                continue
            _push("paragraph", "p",
                  t[:140],
                  f"<p>{_e(t)}</p>",
                  t, depth=4)
            seen_texts.add(t[:200])

    return blocks


def _e(s) -> str:
    """Local HTML-escape helper to keep render-side dependencies minimal."""
    import html
    return html.escape("" if s is None else str(s))
