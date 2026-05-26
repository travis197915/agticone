"""Excel ingestion: pull the claim-id column out of an uploaded `.xlsx`."""
from __future__ import annotations

import io
import logging
from typing import Iterable

logger = logging.getLogger(__name__)

_DEFAULT_ALIASES = ("claim_id", "claimid", "subscriber_id", "subscriberid",
                    "claim id", "subscriber id")


class XlsxParseError(ValueError):
    pass


def extract_claim_ids(
    xlsx_bytes: bytes,
    *,
    claim_id_column: str | None = None,
    sheet_name: str | None = None,
) -> tuple[list[str], str]:
    """Return ``(claim_ids, resolved_column_name)``.

    Header matching is case-insensitive and whitespace-tolerant. When
    ``claim_id_column`` is omitted, we scan for the first header that matches
    one of the known aliases (claim_id, subscriber_id, ...).
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise XlsxParseError("openpyxl is required to parse xlsx files") from exc

    try:
        wb = load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise XlsxParseError(f"failed to open workbook: {exc}") from exc

    ws = wb[sheet_name] if sheet_name else wb[wb.sheetnames[0]]
    rows: Iterable = ws.iter_rows(values_only=True)
    header_row = next(rows, None)
    if not header_row:
        raise XlsxParseError("workbook has no rows")

    headers = [(str(h).strip() if h is not None else "") for h in header_row]
    norm = [h.lower() for h in headers]

    wanted = (claim_id_column or "").strip().lower()
    target_idx = -1
    resolved = ""
    if wanted:
        if wanted in norm:
            target_idx = norm.index(wanted)
            resolved = headers[target_idx]
    if target_idx < 0:
        for alias in _DEFAULT_ALIASES:
            if alias in norm:
                target_idx = norm.index(alias)
                resolved = headers[target_idx]
                break
    if target_idx < 0:
        raise XlsxParseError(
            f"could not find a claim-id column (looked for {claim_id_column!r} "
            f"and aliases {list(_DEFAULT_ALIASES)}); headers found: {headers}"
        )

    claim_ids: list[str] = []
    for row in rows:
        if target_idx >= len(row):
            continue
        cell = row[target_idx]
        if cell is None:
            continue
        value = str(cell).strip()
        if value:
            claim_ids.append(value)

    return claim_ids, resolved
