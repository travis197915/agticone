"""Lightweight remote revision probe — fetch HTML and extract revision metadata."""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import requests

from .revision import normalize_revision_date

log = logging.getLogger(__name__)

_RE_EFF_DATE = re.compile(r"(?:original\s+)?effective\s+date[:\s]*([\d/\-]+)", re.I)
_RE_REV_DATE = re.compile(r"revision\s+date[:\s]*([\d/\-]+)", re.I)
_BIZ_HEADERS = ("platform", "audience", "lob", "line of business", "product", "state", "div")
_USER_AGENT = "UHC-SOP-RevisionCheck/1.0"
_MAX_BYTES = 25 * 1024 * 1024


def _extract_biz_dates(html: str) -> dict[str, str]:
    """Pull revision/effective dates from the first matching biz table."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [
            (cell.get_text(" ", strip=True) or "").lower()
            for cell in header_row.find_all(["th", "td"])
        ]
        if sum(1 for h in headers if any(b in h for b in _BIZ_HEADERS)) < 2:
            continue

        hmap: dict[int, str] = {}
        for i, h in enumerate(headers):
            if "revision" in h and "date" in h:
                hmap[i] = "revision_date"
            elif "effective" in h and "date" in h:
                hmap[i] = "effective_date"

        out: dict[str, str] = {}
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            for idx, field in hmap.items():
                if idx < len(cells):
                    val = cells[idx].get_text(" ", strip=True)
                    if val and field not in out:
                        out[field] = val
        return out
    return {}


def _extract_text_dates(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    m = _RE_EFF_DATE.search(text)
    if m:
        out["effective_date"] = m.group(1)
    m = _RE_REV_DATE.search(text)
    if m:
        out["revision_date"] = m.group(1)
    return out


def probe_remote_sop(url: str, *, timeout: int = 30) -> dict[str, Any]:
    """Fetch a SOP URL and return revision metadata without running the pipeline."""
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "url": url, "error": "unsupported URL scheme"}

    headers = {"User-Agent": _USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, stream=True)
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(65536):
            total += len(chunk)
            if total > _MAX_BYTES:
                return {
                    "ok": False,
                    "url": url,
                    "error": f"response exceeds {_MAX_BYTES} bytes",
                }
            chunks.append(chunk)
        raw = b"".join(chunks)
    except Exception as exc:
        log.warning("revision probe failed for %s: %s", url, exc)
        return {"ok": False, "url": url, "error": str(exc)}

    encoding = resp.encoding or "utf-8"
    try:
        html = raw.decode(encoding, errors="replace")
    except Exception:
        html = raw.decode("utf-8", errors="replace")

    text_dates = _extract_text_dates(html)
    biz_dates = _extract_biz_dates(html)
    revision_date = biz_dates.get("revision_date") or text_dates.get("revision_date", "")
    effective_date = biz_dates.get("effective_date") or text_dates.get("effective_date", "")

    return {
        "ok": True,
        "url": url,
        "content_hash": hashlib.sha256(raw).hexdigest()[:16],
        "revision_date": revision_date,
        "normalized_revision_date": normalize_revision_date(revision_date),
        "effective_date": effective_date,
        "content_type": resp.headers.get("Content-Type", ""),
    }
