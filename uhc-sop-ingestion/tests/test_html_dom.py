"""Tests for the DOM-mirror builder + Neo4j writer.

These run with plain pytest; no Neo4j required. The writer test uses a
recording fake driver to assert which Cypher statements are emitted, in
what order, with what params — that's enough to lock down the
node/edge contract without spinning up a database container.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from uhc_sop_ingestion import html_dom


HERE = Path(__file__).resolve().parent
POC_HTML = (
    HERE.parents[1]
    / "extracted-tools-main"
    / "poc"
    / "obh_facets_timely_filing"
    / "html"
    / "index.html"
)


# ── Fixtures ────────────────────────────────────────────────────────────────


SMALL_HTML = b"""<!DOCTYPE html>
<html><head><title>SOP</title></head><body>
  <section id="introduction">
    <h2>Introduction</h2>
    <h3 id="background">Background</h3>
    <ul>
      <li>First bullet.</li>
      <li><strong>TFF</strong> is 90 days from DOS.</li>
      <li><strong>Exception:</strong> IHC 365 days.</li>
      <li><strong>Note:</strong> Refer to the OBH Facets Medicaid Reclamation.</li>
    </ul>
  </section>
  <section id="procedure">
    <h2>Procedure</h2>
    <p>See <a href="#step-6">Step 6</a> for details.</p>
    <table>
      <tr id="step-6"><th>6</th><td>Does the claim include POTF?</td></tr>
    </table>
  </section>
</body></html>
"""


def _state(html_bytes: bytes) -> dict:
    return {
        "raw_bytes_b64": base64.b64encode(html_bytes).decode("ascii"),
        "doc_format":    "HTML",
    }


# ── block_id stability ──────────────────────────────────────────────────────


def test_block_id_matches_sopingestion_format():
    """Same digest scheme as ``sop_ingestion.html_blocks.block_id_for_html``.
    The SPA exclusion picker and Neo4j HtmlBlock nodes must agree on ids.
    """
    bid = html_dom.block_id_for_html("<li>hi</li>")
    assert bid.startswith("html-")
    assert len(bid) == len("html-") + 12  # 12-char sha1 prefix


def test_block_id_is_content_addressed():
    assert html_dom.block_id_for_html("<p>a</p>") == html_dom.block_id_for_html("<p>a</p>")
    assert html_dom.block_id_for_html("<p>a</p>") != html_dom.block_id_for_html("<p>b</p>")


# ── HTML walk ───────────────────────────────────────────────────────────────


def test_build_dom_tree_returns_nested_structure():
    tree = html_dom.build_dom_tree(_state(SMALL_HTML))
    assert tree["source"] == "html"
    roots = tree["roots"]
    # Two top-level <section>s
    assert len(roots) == 2
    assert all(r["is_root"] for r in roots)
    assert {r["section_id"] for r in roots} == {"introduction", "procedure"}


def test_introduction_background_subtree():
    """The exact slice from the user's screenshot must round-trip 1:1."""
    tree = html_dom.build_dom_tree(_state(SMALL_HTML))
    intro = next(r for r in tree["roots"] if r["section_id"] == "introduction")
    # children = [h2, h3, ul]
    tags = [c["tag"] for c in intro["children"]]
    assert tags == ["h2", "h3", "ul"]

    # The ul has exactly 4 list items in document order.
    ul = intro["children"][2]
    assert ul["tag"] == "ul"
    li_texts = [li["text"] for li in ul["children"]]
    assert li_texts[0].startswith("First bullet")
    assert "TFF" in li_texts[1]
    assert li_texts[2].startswith("Exception")
    assert li_texts[3].startswith("Note")
    # Sibling ordering is preserved
    assert [li["order"] for li in ul["children"]] == [0, 1, 2, 3]


def test_anchor_resolves_to_target_block():
    tree = html_dom.build_dom_tree(_state(SMALL_HTML))
    flat = tree["flat"]
    anchor = next(b for b in flat if b.get("kind") == "anchor")
    assert anchor["fragment"] == "step-6"
    target_block_id = anchor.get("target_block_id")
    assert target_block_id, "anchor should resolve to a block_id"
    # The target is the <tr id="step-6"> we put inside Procedure.
    target = next(b for b in flat if b["block_id"] == target_block_id)
    assert target["tag"] == "tr"
    assert target["section_id"] == "step-6"


def test_flat_list_preserves_parent_pointers():
    tree = html_dom.build_dom_tree(_state(SMALL_HTML))
    flat = tree["flat"]
    by_id = {b["block_id"]: b for b in flat}
    # Every non-root block points at a block that exists in the flat list.
    for b in flat:
        if b["is_root"]:
            assert b["parent_block_id"] == ""
        else:
            assert b["parent_block_id"] in by_id


def test_sub_label_per_kind():
    tree = html_dom.build_dom_tree(_state(SMALL_HTML))
    flat = tree["flat"]
    kinds = {b["tag"]: b["sub_label"] for b in flat}
    assert kinds["section"]   == "HtmlSection"
    assert kinds["h2"]        == "HtmlHeading"
    assert kinds["ul"]        == "HtmlList"
    assert kinds["li"]        == "HtmlListItem"
    assert kinds["table"]     == "HtmlTable"
    assert kinds["tr"]        == "HtmlRow"
    assert kinds["a"]         == "HtmlAnchor"


def test_pure_layout_div_dropped():
    html_bytes = b"<html><body><div><div><p>hi</p></div></div></body></html>"
    flat = html_dom.build_dom_tree(_state(html_bytes))["flat"]
    # The two wrapping <div>s have no heading/section/table/list inside,
    # so they must NOT survive. Only the <p> remains as a root block.
    assert [b["tag"] for b in flat] == ["p"]


# ── Real POC HTML (sanity) ──────────────────────────────────────────────────


@pytest.mark.skipif(not POC_HTML.exists(), reason="POC fixture not available")
def test_poc_timely_filing_html_round_trip():
    tree = html_dom.build_dom_tree(_state(POC_HTML.read_bytes()))
    flat = tree["flat"]
    # The page has many sections — at minimum the Introduction id should
    # show up and its first heading text should be present.
    intro_blocks = [b for b in flat if b.get("section_id") == "introduction"]
    assert intro_blocks, "introduction section must be preserved"
    headings = [b for b in flat if b["kind"] == "heading" and "Background" in b["text"]]
    assert headings, "Background sub-heading must survive"
    # And there must be a <li> whose text starts with 'Exception'.
    ex_lis = [b for b in flat if b["tag"] == "li" and b["text"].startswith("Exception")]
    assert ex_lis, "Exception bullet must be emitted as its own list-item block"


# ── Synthesised tree for DOCX/PDF/XLSX ──────────────────────────────────────


def test_synthesised_tree_for_non_html():
    state = {
        "doc_format": "DOCX",
        "metadata":   {"title": "OBH Reclamation"},
        "pre_sections": [
            {"name": "Background",
             "items": [{"text": "First bullet"}, {"text": "Second bullet"}]}
        ],
        "steps": [
            {"number": 1, "question": "Is the claim timely?", "intro_text": "",
             "decision_rows": [{"condition_if": "Yes", "action": "Process"}]},
        ],
    }
    tree = html_dom.build_dom_tree(state)
    assert tree["source"] == "synthesised"
    roots = tree["roots"]
    assert any(r["section_id"] == "overview"      for r in roots)
    assert any(r["section_id"] == "pre-sections"  for r in roots)
    assert any(r["section_id"] == "steps"         for r in roots)


# ── html_dom_writer Cypher contract (recording fake driver) ────────────────


class _RecordingSession:
    def __init__(self, log: list[tuple[str, dict]]):
        self.log = log

    def execute_write(self, fn):
        fn(self)

    def run(self, query: str, params: dict | None = None):
        self.log.append((query, params or {}))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _RecordingDriver:
    def __init__(self):
        self.log: list[tuple[str, dict]] = []

    def session(self, database: str | None = None, **kw):
        return _RecordingSession(self.log)


@pytest.fixture()
def fake_cfg(monkeypatch):
    """Patch a10_write_neo4j._driver to return a recording fake driver."""
    from uhc_sop_ingestion.agents import a10_write_neo4j

    drv = _RecordingDriver()

    class _Cfg:
        neo4j_database = "neo4j"

    monkeypatch.setattr(a10_write_neo4j, "_driver", lambda cfg: drv)
    return _Cfg(), drv


def test_html_dom_writer_emits_expected_cypher(fake_cfg):
    from uhc_sop_ingestion.agents.a10_write_neo4j import html_dom_writer

    cfg, drv = fake_cfg
    state: dict[str, Any] = {
        "neo4j_sop_id":  "OBH:abc123",
        "raw_bytes_b64": base64.b64encode(SMALL_HTML).decode("ascii"),
        "doc_format":    "HTML",
    }
    out = html_dom_writer(state, cfg)
    assert out["neo4j_html_source"] == "html"
    assert out["neo4j_html_blocks"] > 0
    assert out["neo4j_html_roots"] == 2

    queries = " ;; ".join(q for q, _ in drv.log)
    assert "DETACH DELETE"            in queries
    assert "MERGE (b:HtmlBlock"       in queries
    assert ":HtmlSection"             in queries
    assert ":HtmlListItem"            in queries
    assert "HAS_HTML_BLOCK"           in queries
    assert "MERGE (p)-[r:HAS_CHILD]"  in queries
    assert "MERGE (a)-[:NEXT_SIBLING]" in queries
    assert "MERGE (a)-[r:HREF]"       in queries


def test_html_dom_writer_noops_without_sop_id(fake_cfg):
    from uhc_sop_ingestion.agents.a10_write_neo4j import html_dom_writer

    cfg, drv = fake_cfg
    out = html_dom_writer({"raw_bytes_b64": "", "doc_format": "HTML"}, cfg)
    assert out == {}
    assert drv.log == []
