"""Revision-date and canonical-URL helpers for SOP versioning."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse, urlunparse

_RE_DATE_TOKEN = re.compile(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})")


def normalize_canonical_url(url: str) -> str:
    """Stable document identity: scheme + host + path, no fragment/query."""
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    path = unquote(parsed.path or "/").rstrip("/") or "/"
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        path,
        "",
        "",
        "",
    ))


def normalize_revision_date(raw: str) -> str:
    """Normalize common UHC date strings to ISO ``YYYY-MM-DD`` when possible."""
    text = (raw or "").strip()
    if not text:
        return ""
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if iso:
        return text
    m = _RE_DATE_TOKEN.search(text)
    if not m:
        return text
    month, day, year = m.group(1), m.group(2), m.group(3)
    if len(year) == 2:
        year = f"20{year}" if int(year) < 70 else f"19{year}"
    try:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except ValueError:
        return text


def revision_dates_equal(a: str, b: str) -> bool:
    """Compare revision dates after normalization."""
    na, nb = normalize_revision_date(a), normalize_revision_date(b)
    if na and nb:
        return na == nb
    return (a or "").strip().lower() == (b or "").strip().lower()
