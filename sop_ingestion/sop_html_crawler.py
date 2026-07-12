"""Crawl SOP source HTML with BeautifulSoup and cache it in MongoDB.

The claim-detail UI's "View SOP" opens the *original* SOP template (the raw
HTML with the rules), not the knowledge-graph viewer. Rather than hot-linking
the source URL on every click (which is slow and would go through the guarded
relay), each HTML SOP is crawled once, its stylesheet inlined so the page is
fully self-contained, and the result stored in a dedicated Mongo collection.

Only HTTP(S) HTML SOPs are crawled. PDF uploads (``file://…​.pdf``) and
node/YAML SOPs (``yaml://…``) are ignored.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Dedicated collection — kept separate from the ingestion/audit collections.
COLLECTION = "sop_source_html"
_HEADERS = {"User-Agent": "uhc-sop-html-crawler/1.0"}
_TIMEOUT = 20
_MAX_PAGES = 25


def _mongo_collection():
    """Return the dedicated ``sop_source_html`` collection, or ``None``.

    Mirrors the connection pattern in ``sop_ir.persist`` / ``rule_reconcile``.
    """
    try:
        from pymongo import MongoClient

        from sop_backend.db_config import is_prod, mongo_uri_from_env

        if not (os.environ.get("MONGO_HOST") or (is_prod() and os.environ.get("MONGO_URI"))):
            return None
        database = os.environ.get("MONGO_DATABASE")
        client = MongoClient(mongo_uri_from_env(), serverSelectionTimeoutMS=10000)
        return client[database][COLLECTION]
    except Exception as exc:  # pragma: no cover - infra dependent
        logger.warning("sop_html_crawler: Mongo unavailable (%s)", exc)
        return None


def is_crawlable_url(url: str | None) -> bool:
    """True only for HTTP(S) HTML SOPs. PDFs and yaml:// SOPs are ignored."""
    u = (url or "").strip().lower()
    if not u.startswith(("http://", "https://")):
        return False
    if u.endswith(".pdf"):
        return False
    return True


def classify_sop_source(url: str | None) -> str:
    """Coarse SOP source kind for the UI: ``html`` | ``pdf`` | ``node``."""
    u = (url or "").strip().lower()
    if u.startswith(("http://", "https://")) and not u.endswith(".pdf"):
        return "html"
    if u.endswith(".pdf") or u.startswith("file://"):
        return "pdf"
    return "node"


def _same_prefix(base: str, target: str) -> bool:
    """True when ``target`` lives under the same host + directory as ``base``."""
    b, t = urlparse(base), urlparse(target)
    if b.netloc != t.netloc:
        return False
    base_dir = base.rsplit("/", 1)[0]
    return target.startswith(base_dir)


def _inline_stylesheets(soup: BeautifulSoup, page_url: str, session: requests.Session) -> None:
    """Fetch <link rel=stylesheet> hrefs and inline them as <style> so the
    stored HTML renders identically without any external requests."""
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        rel = [r.lower() for r in (rel if isinstance(rel, list) else [rel])]
        if "stylesheet" not in rel:
            continue
        href = link.get("href")
        if not href:
            continue
        try:
            resp = session.get(urljoin(page_url, href), headers=_HEADERS, timeout=_TIMEOUT)
            if resp.ok:
                style = soup.new_tag("style")
                style.string = resp.text
                link.replace_with(style)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("inline css failed %s (%s)", href, exc)


def _absolutize(soup: BeautifulSoup, page_url: str) -> None:
    """Rewrite relative <img src>/<a href> to absolute so they resolve when the
    stored HTML is rendered from a blob/new tab."""
    for tag, attr in (("img", "src"), ("a", "href")):
        for el in soup.find_all(tag):
            val = el.get(attr)
            if val and not val.startswith(("#", "http://", "https://", "data:", "mailto:", "tel:")):
                el[attr] = urljoin(page_url, val)


def crawl_sop_html(start_url: str, max_pages: int = _MAX_PAGES) -> dict:
    """BFS-crawl an HTML SOP starting at ``start_url``.

    Follows only same-host, same-directory ``.html`` links (SOPs are usually a
    single self-contained ``index.html``, but multi-page SOPs are supported).
    Scripts are stripped and stylesheets inlined. Returns
    ``{"html", "pages", "page_count"}``.
    """
    session = requests.Session()
    seen: set[str] = set()
    pages: list[dict[str, str]] = []
    queue: list[str] = [urldefrag(start_url)[0]]

    while queue and len(pages) < max_pages:
        page_url = queue.pop(0)
        if page_url in seen:
            continue
        seen.add(page_url)
        try:
            resp = session.get(page_url, headers=_HEADERS, timeout=_TIMEOUT)
        except Exception as exc:
            logger.warning("crawl fetch failed %s (%s)", page_url, exc)
            continue
        if not resp.ok or "html" not in resp.headers.get("Content-Type", "").lower():
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Discover same-prefix sub-pages before we mutate the tree.
        for a in soup.find_all("a", href=True):
            nxt = urldefrag(urljoin(page_url, a["href"]))[0]
            if (
                nxt not in seen
                and (nxt.lower().endswith(".html") or nxt.endswith("/"))
                and _same_prefix(start_url, nxt)
            ):
                queue.append(nxt)

        for junk in soup(["script", "noscript"]):
            junk.decompose()
        _inline_stylesheets(soup, page_url, session)
        _absolutize(soup, page_url)
        pages.append({"url": page_url, "html": str(soup)})

    if len(pages) == 1:
        combined = pages[0]["html"]
    else:
        parts: list[str] = []
        for p in pages:
            body = BeautifulSoup(p["html"], "html.parser").body
            inner = body.decode_contents() if body else p["html"]
            parts.append(f'<section data-sop-page="{p["url"]}">{inner}</section>')
        combined = "\n".join(parts)

    return {"html": combined, "pages": [p["url"] for p in pages], "page_count": len(pages)}


def crawl_and_store(sop) -> dict | None:
    """Crawl a single ``AuditSop``'s HTML and upsert it into Mongo.

    Returns the stored document, or ``None`` when the SOP is not a crawlable
    HTML source (PDF / node/YAML) or the crawl produced nothing.
    """
    if not is_crawlable_url(getattr(sop, "url", None)):
        return None
    result = crawl_sop_html(sop.url)
    if not result.get("html"):
        return None
    doc = {
        "sop_id": int(sop.id),
        "title": sop.title or "",
        "source_url": sop.url or "",
        "html": result["html"],
        "pages": result["pages"],
        "page_count": result["page_count"],
        "byte_size": len(result["html"]),
        "crawled_at": datetime.now(timezone.utc).isoformat(),
    }
    col = _mongo_collection()
    if col is not None:
        col.replace_one({"sop_id": doc["sop_id"]}, doc, upsert=True)
    else:
        logger.warning("sop_html_crawler: Mongo unavailable — not stored (sop_id=%s)", sop.id)
    return doc


def get_stored(sop_id: int) -> dict | None:
    """Fetch a previously crawled SOP HTML doc from Mongo (without ``_id``)."""
    col = _mongo_collection()
    if col is None:
        return None
    return col.find_one({"sop_id": int(sop_id)}, {"_id": 0})
