"""Tests for revision probe and scheduler classification."""
from uhc_sop_ingestion.revision_probe import _extract_biz_dates, _extract_text_dates
from uhc_sop_ingestion.revision import normalize_revision_date

from sop_ingestion.services.revision_scheduler import TrackedSop, _classify_probe
from sop_ingestion.services.versioning import VERSION_CONTENT_CHANGE, VERSION_REVISED, VERSION_UNCHANGED


def test_extract_text_dates_from_html_snippet():
    html = "<html><body>Revision Date: 06/01/2025 Effective Date: 01/01/2024</body></html>"
    dates = _extract_text_dates(html)
    assert dates["revision_date"] == "06/01/2025"
    assert dates["effective_date"] == "01/01/2024"


def test_extract_biz_table_revision_column():
    html = """
    <table>
      <tr><th>Platform</th><th>Audience</th><th>Revision Date</th></tr>
      <tr><td>Facets</td><td>Examiners</td><td>03/15/2025</td></tr>
    </table>
    """
    dates = _extract_biz_dates(html)
    assert dates["revision_date"] == "03/15/2025"


def test_classify_probe_unchanged():
    tracked = TrackedSop(
        document_id=1,
        canonical_url="https://example.com/sop.html",
        fetch_url="https://example.com/sop.html",
        stored_revision_date="01/15/2024",
        stored_content_hash="abc123",
        workflow_id=None,
        max_depth=4,
        max_docs=200,
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-5-20250929",
    )
    probe = {
        "revision_date": "01/15/2024",
        "normalized_revision_date": normalize_revision_date("01/15/2024"),
        "content_hash": "abc123",
    }
    assert _classify_probe(tracked, probe) == VERSION_UNCHANGED


def test_classify_probe_revised():
    tracked = TrackedSop(
        document_id=1,
        canonical_url="https://example.com/sop.html",
        fetch_url="https://example.com/sop.html",
        stored_revision_date="01/15/2024",
        stored_content_hash="abc123",
        workflow_id=None,
        max_depth=4,
        max_docs=200,
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-5-20250929",
    )
    probe = {
        "revision_date": "06/01/2025",
        "normalized_revision_date": normalize_revision_date("06/01/2025"),
        "content_hash": "def456",
    }
    assert _classify_probe(tracked, probe) == VERSION_REVISED


def test_classify_probe_content_change_same_revision():
    tracked = TrackedSop(
        document_id=1,
        canonical_url="https://example.com/sop.html",
        fetch_url="https://example.com/sop.html",
        stored_revision_date="01/15/2024",
        stored_content_hash="abc123",
        workflow_id=None,
        max_depth=4,
        max_docs=200,
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-5-20250929",
    )
    probe = {
        "revision_date": "01/15/2024",
        "normalized_revision_date": normalize_revision_date("01/15/2024"),
        "content_hash": "def456",
    }
    assert _classify_probe(tracked, probe) == VERSION_CONTENT_CHANGE
