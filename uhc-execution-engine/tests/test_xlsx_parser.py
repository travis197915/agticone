"""Tests for Excel claim row extraction."""
from __future__ import annotations

import io
from datetime import datetime

import pytest
from openpyxl import Workbook

from uhc_execution_engine.xlsx_parser import extract_claim_ids, extract_claim_rows


def _workbook_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_extract_claim_rows_with_billing_columns():
    data = _workbook_bytes([
        ["claim_id", "PAID_DT", "Total_Billed", "Total_Paid"],
        ["CLM001", datetime(2026, 1, 15), 1200.50, 800],
        ["CLM002", None, 500, 0],
    ])
    rows, col = extract_claim_rows(data)
    assert col == "claim_id"
    assert len(rows) == 2
    assert rows[0]["claim_id"] == "CLM001"
    assert rows[0]["paid_dt"] == "2026-01-15"
    assert rows[0]["total_billed"] == 1200.5
    assert rows[0]["total_paid"] == 800
    assert rows[1]["claim_id"] == "CLM002"
    assert "paid_dt" not in rows[1]
    assert rows[1]["total_billed"] == 500
    assert rows[1]["total_paid"] == 0


def test_extract_claim_ids_backward_compatible():
    data = _workbook_bytes([
        ["subscriber_id", "Total Billed"],
        ["SUB-9", 99],
    ])
    ids, col = extract_claim_ids(data)
    assert col == "subscriber_id"
    assert ids == ["SUB-9"]


def test_extract_claim_rows_audit_sts_and_auditor_name():
    data = _workbook_bytes([
        ["ClaimID", "AuditorName", "Audit_Sts", "Audit_Sts_DT"],
        ["CLM100", "Jane Auditor", "Clean", datetime(2025, 10, 8)],
        ["CLM101", None, "Defect", None],
    ])
    rows, col = extract_claim_rows(data)
    assert col == "ClaimID"
    assert len(rows) == 2
    assert rows[0]["claim_id"] == "CLM100"
    assert rows[0]["original_auditor"] == "Jane Auditor"
    assert rows[0]["auditor_status"] == "CLEAN"
    assert rows[1]["claim_id"] == "CLM101"
    assert "original_auditor" not in rows[1]
    assert rows[1]["auditor_status"] == "DEFECT"
