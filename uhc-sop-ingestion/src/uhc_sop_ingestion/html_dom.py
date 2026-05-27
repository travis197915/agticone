"""
html_dom.py — DOM-mirror builder for the Neo4j HTML graph
=========================================================

Produces a *nested* tree of structural blocks that mirrors the source HTML
DOM exactly, so the Neo4j writer can lay it out in the graph as:

    (:SopDocument)
        ─[:HAS_HTML_BLOCK]→ (:HtmlSection {is_root:true})
              ─[:HAS_CHILD {order}]→ (:HtmlHeading)
              ─[:HAS_CHILD {order}]→ (:HtmlList)
                    ─[:HAS_CHILD {order}]→ (:HtmlListItem) …

Identity is content-addressed via :func:`block_id_for_html`, the same
``sha1(outerHTML)[:12]`` scheme used by
``sop_ingestion.html_blocks.block_id_for_html`` so the SPA exclusion
picker, the Postgres ``SopExclusion`` rows and the Neo4j HtmlBlock nodes
all share the same id space.

Two source paths:

* HTML SOPs (``state['doc_format'] == 'HTML'``): walk BeautifulSoup over
  ``state['raw_bytes_b64']`` and emit one block per structural tag.
* Non-HTML SOPs: synthesise an HTML-shaped tree from the already-parsed
  ``pre_sections`` / ``steps`` / ``group_rules`` / ``reference_tables``
  fields on the state. The structure mirrors the DOCX/PDF/XLSX as if
  someone had rendered it to HTML.

Both paths produce the same shape; the writer agent doesn't care which
ran.
"""
from __future__ import annotations

import base64
import hashlib
import html as _html_module
import logging
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .state import PipelineState

logger = logging.getLogger(__name__)


# ── Public id helper ─────────────────────────────────────────────────────────


def block_id_for_html(html: str) -> str:
    """Stable, content-addressed id for an HTML block.

    Matches ``sop_ingestion.html_blocks.block_id_for_html`` byte-for-byte
    so the SPA exclusion picker, the Postgres
    ``SopExclusion.target_key`` and the Neo4j ``HtmlBlock.block_id`` are
    interchangeable.
    """
    digest = hashlib.sha1((html or "").encode("utf-8", "replace")).hexdigest()
    return f"html-{digest[:12]}"


# ── BS4 walk: tag -> (kind, sub_label, depth_for_headings) ───────────────────


# Every key here becomes a node. The order matters only for label assignment.
_BLOCK_TAGS: dict[str, tuple[str, str, int]] = {
    "section":    ("section",   "HtmlSection",    0),
    "article":    ("section",   "HtmlSection",    0),
    "h1":         ("heading",   "HtmlHeading",    1),
    "h2":         ("heading",   "HtmlHeading",    2),
    "h3":         ("heading",   "HtmlHeading",    3),
    "h4":         ("heading",   "HtmlHeading",    4),
    "h5":         ("heading",   "HtmlHeading",    5),
    "h6":         ("heading",   "HtmlHeading",    6),
    "ul":         ("list",      "HtmlList",       0),
    "ol":         ("list",      "HtmlList",       0),
    "li":         ("list_item", "HtmlListItem",   0),
    "table":      ("table",     "HtmlTable",      0),
    "thead":      ("row_group", "HtmlRowGroup",   0),
    "tbody":      ("row_group", "HtmlRowGroup",   0),
    "tfoot":      ("row_group", "HtmlRowGroup",   0),
    "tr":         ("row",       "HtmlRow",        0),
    "th":         ("cell",      "HtmlCell",       0),
    "td":         ("cell",      "HtmlCell",       0),
    "p":          ("paragraph", "HtmlParagraph",  0),
    "blockquote": ("callout",   "HtmlCallout",    0),
    "pre":        ("code",      "HtmlCode",       0),
    "a":          ("anchor",    "HtmlAnchor",     0),
    "div":        ("section",   "HtmlSection",    0),
    "nav":        ("section",   "HtmlSection",    0),
}

_SKIP_TAGS = {"script", "style", "noscript", "head", "meta", "link"}

# When deciding whether to keep a <div> as a node, require it to actually
# contain something heading-like or a recognisable sectioning role. Bare
# layout divs would otherwise explode the node count without adding value.
_KEEP_DIV_IF_HAS = ("section", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol")

# Anchors that don't have an href or whose text is empty are skipped.
_ANCHOR_HREF_RE = re.compile(r"^[^#\s].*|^#[\w\-]+$")


def _soup(state: "PipelineState"):
    """Return a BS4 soup over ``state['raw_bytes_b64']`` or None."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is in requirements
        return None
    b64 = state.get("raw_bytes_b64", "") or ""
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception as exc:
        logger.warning("html_dom: base64 decode failed: %s", exc)
        return None
    try:
        return BeautifulSoup(raw, "lxml")
    except Exception:
        try:
            return BeautifulSoup(raw, "html.parser")
        except Exception as exc:
            logger.warning("html_dom: bs4 parse failed: %s", exc)
            return None


_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


def _label_for(kind: str, el, text: str) -> str:
    if kind == "heading":
        return text or el.name.upper()
    if kind == "anchor":
        return text or (el.get("href") or "")[:120]
    if kind == "table":
        cap = el.find("caption")
        if cap and cap.get_text(strip=True):
            return f"Table — {cap.get_text(strip=True)}"[:220]
        first_row = el.find("tr")
        if first_row:
            cells = [c.get_text(" ", strip=True)
                     for c in first_row.find_all(["th", "td"])]
            cells = [c for c in cells if c]
            if cells:
                return ("Table — " + " | ".join(cells[:4]))[:220]
        return "Table"
    if kind == "list":
        first = el.find("li")
        if first:
            return f"List · {first.get_text(' ', strip=True)[:140]}"
        return "List"
    if kind == "list_item":
        return text[:200] or "List item"
    if kind == "row":
        return text[:200] or "Row"
    if kind == "cell":
        return text[:200] or "Cell"
    if kind == "section":
        # Prefer the first heading inside, else the element id.
        for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            h = el.find(tag)
            if h and h.get_text(strip=True):
                return h.get_text(strip=True)[:200]
        sid = el.get("id", "")
        if sid:
            return sid
        return text[:200] or el.name
    return text[:200] or el.name


def _block_for(el, *, anchors: dict[str, str]) -> dict | None:
    """Build a single block dict for ``el``, registering its anchor id."""
    name = el.name
    if name not in _BLOCK_TAGS:
        return None
    if name == "div":
        # Drop pure layout divs (no headings, sectioning, tables, lists).
        if not el.find(_KEEP_DIV_IF_HAS):
            return None
    if name == "a":
        href = (el.get("href") or "").strip()
        text = el.get_text(" ", strip=True)
        if not href and not text:
            return None
        if not _ANCHOR_HREF_RE.match(href or "#x"):
            # Drop javascript:/mailto: noise — but keep #fragment + http(s) hrefs.
            if not (href.startswith("#") or href.startswith(("http://", "https://", "/"))):
                return None

    kind, sub_label, depth = _BLOCK_TAGS[name]
    html_outer = str(el)
    text = _norm(el.get_text(" ", strip=True))
    bid = block_id_for_html(html_outer)
    block: dict[str, Any] = {
        "block_id":     bid,
        "tag":          name,
        "kind":         kind,
        "sub_label":    sub_label,
        "depth":        depth,
        "section_id":   el.get("id", "") or "",
        "label":        _label_for(kind, el, text),
        "text":         text[:4000],
        "html_snippet": html_outer[:2000],
        "children":     [],
        "is_root":      False,
    }
    if name == "a":
        href = (el.get("href") or "").strip()
        block["href"] = href
        if href.startswith("#"):
            block["fragment"] = href[1:]
        elif href.startswith(("http://", "https://", "/")):
            block["external_url"] = href

    # Register anchor target id for in-page link resolution.
    if el.get("id"):
        anchors[el["id"]] = bid

    return block


def _walk(el, anchors: dict[str, str]) -> list[dict]:
    """Depth-first DOM walk returning a flat list of *direct* block children.

    Recurses through non-block tags transparently, but stops descending once
    a block tag has been emitted (its children are attached to it instead).
    """
    name = getattr(el, "name", None)
    if name is None or name in _SKIP_TAGS:
        return []
    block = _block_for(el, anchors=anchors) if name in _BLOCK_TAGS else None

    children_blocks: list[dict] = []
    for child in getattr(el, "children", []) or []:
        if getattr(child, "name", None) is None:
            continue
        children_blocks.extend(_walk(child, anchors))

    if block is None:
        return children_blocks

    # Assign sibling order
    for i, c in enumerate(children_blocks):
        c["order"] = i
    block["children"] = children_blocks
    return [block]


def _resolve_anchors(roots: list[dict], anchors: dict[str, str]) -> None:
    """Fill ``target_block_id`` / ``unresolved`` on every anchor block."""
    def _visit(blocks: list[dict]) -> None:
        for b in blocks:
            if b.get("kind") == "anchor":
                frag = b.get("fragment")
                if frag:
                    target = anchors.get(frag)
                    b["target_block_id"] = target or ""
                    b["unresolved"] = target is None
            if b.get("children"):
                _visit(b["children"])
    _visit(roots)


def _flatten(roots: list[dict]) -> list[dict]:
    """Return blocks in document order, each augmented with a
    ``parent_block_id`` field so callers can persist a flat table cheaply."""
    out: list[dict] = []

    def _visit(blocks: list[dict], parent_id: str, depth: int) -> None:
        for i, b in enumerate(blocks):
            b["parent_block_id"] = parent_id
            b["tree_depth"] = depth
            if "order" not in b:
                b["order"] = i
            out.append(b)
            if b.get("children"):
                _visit(b["children"], b["block_id"], depth + 1)
    _visit(roots, "", 0)
    return out


# ─── Public API ──────────────────────────────────────────────────────────────


def build_dom_tree(state: "PipelineState") -> dict:
    """Build the DOM-mirror tree for the SOP in ``state``.

    Returns::

        {
            "roots":      [ { block_id, children, ... }, ... ],
            "flat":       [ {block_id, parent_block_id, order, tree_depth, ...} ],
            "anchors":    { fragment -> block_id },
            "source":     "html" | "synthesised",
        }

    Empty dict-style result is fine; the writer agent treats ``roots == []``
    as a no-op.
    """
    fmt = (state.get("doc_format") or "HTML").upper()

    if fmt == "HTML":
        soup = _soup(state)
        if soup is None:
            return {"roots": [], "flat": [], "anchors": {}, "source": "html"}
        body = soup.body or soup
        anchors: dict[str, str] = {}
        # Walk the body's children — body itself is not a block tag, so its
        # children become the roots.
        roots: list[dict] = []
        for child in body.children:
            if getattr(child, "name", None) is None:
                continue
            roots.extend(_walk(child, anchors))
        for i, r in enumerate(roots):
            r["is_root"] = True
            r["order"] = i
        _resolve_anchors(roots, anchors)
        flat = _flatten(roots)
        return {"roots": roots, "flat": flat, "anchors": anchors, "source": "html"}

    # Non-HTML SOP — synthesise from parsed state.
    roots = _synthesise_from_state(state)
    anchors = {}
    _resolve_anchors(roots, anchors)
    flat = _flatten(roots)
    return {"roots": roots, "flat": flat, "anchors": anchors, "source": "synthesised"}


# ─── DOCX / PDF / XLSX fallback: synthesise an HTML-shaped tree ──────────────


def _e(s: Any) -> str:
    return _html_module.escape("" if s is None else str(s))


def _mk_block(
    *, tag: str, label: str, text: str = "", html: str | None = None,
    section_id: str = "", children: list[dict] | None = None,
) -> dict:
    kind, sub_label, depth = _BLOCK_TAGS[tag]
    outer = html or f"<{tag}>{_e(text or label)}</{tag}>"
    bid = block_id_for_html(outer)
    return {
        "block_id":     bid,
        "tag":          tag,
        "kind":         kind,
        "sub_label":    sub_label,
        "depth":        depth,
        "section_id":   section_id,
        "label":        label[:220],
        "text":         (text or label)[:4000],
        "html_snippet": outer[:2000],
        "children":     list(children or []),
        "is_root":      False,
    }


def _synthesise_from_state(state: "PipelineState") -> list[dict]:
    roots: list[dict] = []
    meta = state.get("metadata") or {}

    # 1. Document header
    title = meta.get("title") or "SOP"
    header_children = [
        _mk_block(tag="h1", label=title, text=title),
    ]
    summary = (state.get("llm_summary") or meta.get("summary") or "").strip()
    if summary:
        header_children.append(_mk_block(tag="p", label="Summary", text=summary))
    roots.append(_mk_block(
        tag="section", label="Overview", section_id="overview",
        children=header_children,
    ))

    # 2. Pre-sections → section > ul > li
    pre_secs = state.get("pre_sections") or []
    if pre_secs:
        ps_children: list[dict] = []
        for ps in pre_secs:
            name = ps.get("name") or "Section"
            items = ps.get("items") or []
            li_children: list[dict] = []
            for it in items:
                text = it.get("text", "") if isinstance(it, dict) else str(it)
                if not text:
                    continue
                li_children.append(_mk_block(tag="li", label=text, text=text))
            section_children: list[dict] = [
                _mk_block(tag="h3", label=name, text=name),
            ]
            if li_children:
                section_children.append(
                    _mk_block(tag="ul", label="Items", children=li_children),
                )
            ps_children.append(_mk_block(
                tag="section", label=name,
                section_id=ps.get("section_id", "") or "",
                children=section_children,
            ))
        roots.append(_mk_block(
            tag="section", label="Pre-sections", section_id="pre-sections",
            children=ps_children,
        ))

    # 3. Steps → section per step > intro paragraph + decision table
    steps = state.get("enriched_steps") or state.get("steps") or []
    if steps:
        step_children: list[dict] = []
        for step in steps:
            num = step.get("number", step.get("step_number"))
            q = step.get("question") or ""
            head = f"Step {num}" + (f" — {q}" if q else "")
            intro = (step.get("intro_text") or step.get("narrative_context") or "").strip()
            inner: list[dict] = [_mk_block(tag="h3", label=head, text=head)]
            if intro:
                inner.append(_mk_block(tag="p", label="Intro", text=intro))
            rows = step.get("decision_rows") or step.get("rows") or []
            if rows:
                tr_children: list[dict] = [
                    _mk_block(tag="tr", label="Header", children=[
                        _mk_block(tag="th", label="If", text="If"),
                        _mk_block(tag="th", label="Then", text="Then"),
                        _mk_block(tag="th", label="Type", text="Type"),
                    ]),
                ]
                for dec in rows:
                    if not isinstance(dec, dict):
                        continue
                    cond = (dec.get("condition_if") or dec.get("if")
                            or dec.get("condition", "")) or ""
                    act = (dec.get("action_text") or dec.get("then")
                           or dec.get("action", "")) or ""
                    dtype = (dec.get("decision_type") or dec.get("decision") or "")
                    tr_children.append(_mk_block(tag="tr", label=cond[:120], children=[
                        _mk_block(tag="td", label="If", text=cond),
                        _mk_block(tag="td", label="Then", text=act),
                        _mk_block(tag="td", label="Type", text=str(dtype)),
                    ]))
                inner.append(_mk_block(
                    tag="table", label=f"Decisions — {head}",
                    children=tr_children,
                ))
            step_children.append(_mk_block(
                tag="section", label=head,
                section_id=f"step-{num}",
                children=inner,
            ))
        roots.append(_mk_block(
            tag="section", label="Steps", section_id="steps",
            children=step_children,
        ))

    # 4. Group limits → section > table
    groups = state.get("group_rules") or []
    if groups:
        tr_children = [
            _mk_block(tag="tr", label="Header", children=[
                _mk_block(tag="th", label="Group", text="Group"),
                _mk_block(tag="th", label="Network", text="Network"),
                _mk_block(tag="th", label="Limit", text="Limit"),
            ]),
        ]
        for g in groups:
            name = g.get("group_name", "")
            net = g.get("network_type", "")
            limit = (f"{g.get('limit_days')} days" if g.get("limit_days")
                     else f"{g.get('limit_months')} months" if g.get("limit_months")
                     else "")
            tr_children.append(_mk_block(tag="tr", label=name, children=[
                _mk_block(tag="td", label="Group", text=name),
                _mk_block(tag="td", label="Network", text=net),
                _mk_block(tag="td", label="Limit", text=limit),
            ]))
        roots.append(_mk_block(
            tag="section", label="Group Limits", section_id="group-limits",
            children=[
                _mk_block(tag="h2", label="Group Limits", text="Group Limits"),
                _mk_block(tag="table", label="Group Limits",
                          children=tr_children),
            ],
        ))

    # 5. Reference tables (POTF lists etc.)
    ref_tbls = state.get("reference_tables") or []
    if ref_tbls:
        ref_children: list[dict] = [
            _mk_block(tag="h2", label="References", text="References"),
        ]
        for rt in ref_tbls:
            rt_name = rt.get("name") or "Reference"
            items = rt.get("rows") or rt.get("items") or []
            li_children = [
                _mk_block(tag="li", label=(it.get("content", "") if isinstance(it, dict) else str(it)),
                          text=(it.get("content", "") if isinstance(it, dict) else str(it)))
                for it in items
                if (it.get("content") if isinstance(it, dict) else str(it))
            ]
            ref_children.append(_mk_block(
                tag="section", label=rt_name,
                children=[
                    _mk_block(tag="h3", label=rt_name, text=rt_name),
                    _mk_block(tag="ul", label="Items", children=li_children),
                ],
            ))
        roots.append(_mk_block(
            tag="section", label="References", section_id="references",
            children=ref_children,
        ))

    for i, r in enumerate(roots):
        r["is_root"] = True
        r["order"] = i
    return roots
