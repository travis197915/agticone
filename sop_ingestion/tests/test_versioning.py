"""Tests for SOP revision-date normalization."""
from uhc_sop_ingestion.revision import (
    normalize_canonical_url,
    normalize_revision_date,
    revision_dates_equal,
)


def test_normalize_canonical_url_strips_query_and_fragment():
    url = "HTTPS://Example.COM/path/doc.html?x=1#section"
    assert normalize_canonical_url(url) == "https://example.com/path/doc.html"


def test_normalize_revision_date_to_iso():
    assert normalize_revision_date("01/15/2024") == "2024-01-15"
    assert normalize_revision_date("Revision Date: 3/2/25") == "2025-03-02"


def test_revision_dates_equal_with_different_formats():
    assert revision_dates_equal("01/15/2024", "2024-01-15")
