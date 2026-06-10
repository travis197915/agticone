"""Run a single PDF through the dedicated PDF agent army and render the
workflow that the auto-builder would generate — without touching any database.

Usage:
    PYTHONPATH=. python scripts/pdf_workflow_demo.py OBH_Facets_Timely_Filing.pdf

Outisde of the canvas DB, this mirrors builder.sop_autobuild's node-decomposition
rules (diamond for decision points, round-rectangle for terminals, rectangle
otherwise; >6 rules → numbered parts) so the rendered HTML reflects the real
generated workflow.
"""
from __future__ import annotations

import base64
import html
import json
import sys
from pathlib import Path

from uhc_sop_ingestion.config import PipelineConfig
from uhc_sop_ingestion.agents.a06_parse_pdf import (
    pdf_text_extractor, pdf_metadata, pdf_step_inventory, pdf_raw_text_normalizer)
from uhc_sop_ingestion.agents import a06b_pdf_agentic as army

_MAX_RULES_PER_NODE = 6
_COL_W, _ROW_H, _SHAPE_W, _SHAPE_H = 360.0, 150.0, 250.0, 96.0
_ZIG = (0.0, 170.0)


def _split_decisions(rows: list[dict]) -> list[tuple[str, list]]:
    if len(rows) > _MAX_RULES_PER_NODE:
        return [(f"part {i+1}", rows[i:i+_MAX_RULES_PER_NODE])
                for i in range(0, len(rows), _MAX_RULES_PER_NODE)]
    return [("", rows)]


def run(pdf_path: str) -> dict:
    cfg = PipelineConfig.from_env()
    data = Path(pdf_path).read_bytes()
    state: dict = {
        "doc_format": "PDF",
        "current_url": f"file://{Path(pdf_path).resolve()}",
        "raw_bytes_b64": base64.b64encode(data).decode(),
        "metadata": {},
    }

    parse_chain = [pdf_text_extractor, pdf_metadata,
                   pdf_step_inventory, pdf_raw_text_normalizer]
    army_chain = [
        army.pdf_document_profiler, army.pdf_step_extractor,
        army.pdf_question_refiner, army.pdf_decision_normalizer,
        army.pdf_code_grounder, army.pdf_routing_resolver,
        army.pdf_terminal_marker, army.pdf_quality_gate,
    ]
    for fn in parse_chain + army_chain:
        delta = fn(state, cfg) or {}
        state.update(delta)
        if fn in army_chain:
            print(f"  ✓ {fn.__name__:<26} steps={len(state.get('steps') or [])}")
    return state


def to_nodes(state: dict) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    prev = None
    row = 0
    for step in state.get("steps") or []:
        rows = step.get("decision_rows") or []
        for group_label, bucket in (_split_decisions(rows) if rows else [("", [])]):
            if step.get("is_terminal"):
                shape = "round-rectangle"
            elif len(bucket) >= 2:
                shape = "diamond"
            else:
                shape = "rectangle"
            label = f"Step {step['number']}"
            if step.get("question"):
                label += f": {step['question']}"
            if group_label:
                label += f" — {group_label}"
            nid = f"n{len(nodes)}"
            nodes.append({
                "id": nid, "shape": shape, "label": label,
                "x": _ZIG[row % 2], "y": (row + 1) * _ROW_H,
                "rules": bucket, "terminal": bool(step.get("is_terminal")),
            })
            if prev is not None:
                edges.append({"from": prev, "to": nid})
            prev = nid
            row += 1
    return nodes, edges


def render_html(state: dict, nodes: list[dict], edges: list[dict]) -> str:
    meta = state.get("metadata") or {}
    title = meta.get("title") or "SOP"
    width = _SHAPE_W + max((n["x"] for n in nodes), default=0) + 120
    height = max((n["y"] for n in nodes), default=0) + _SHAPE_H + 120
    pos = {n["id"]: n for n in nodes}

    svg_lines = []
    for e in edges:
        a, b = pos[e["from"]], pos[e["to"]]
        x1 = a["x"] + _SHAPE_W / 2
        y1 = a["y"] + _SHAPE_H
        x2 = b["x"] + _SHAPE_W / 2
        y2 = b["y"]
        midy = (y1 + y2) / 2
        svg_lines.append(
            f'<path d="M{x1},{y1} C{x1},{midy} {x2},{midy} {x2},{y2}" '
            f'fill="none" stroke="#7c8aa5" stroke-width="2" marker-end="url(#arrow)"/>')

    node_divs = []
    for n in nodes:
        rules_html = ""
        for r in n["rules"]:
            cond = " AND ".join(p for p in [r.get("condition_if", ""),
                                            r.get("condition_and", "")] if p)
            act = r.get("action", "")
            codes = ", ".join(r.get("codes") or [])
            skip = r.get("skip_to_step")
            badge = f'<span class="dt dt-{(r.get("decision") or "NA").lower()}">{html.escape(r.get("decision") or "—")}</span>'
            line = badge
            if cond:
                line += f' <b>IF</b> {html.escape(cond)}'
            if act:
                line += f' <b>→</b> {html.escape(act)}'
            if codes:
                line += f' <span class="code">[{html.escape(codes)}]</span>'
            if skip:
                line += f' <span class="goto">↦ step {skip}</span>'
            rules_html += f'<li>{line}</li>'
        shape_cls = n["shape"].replace("round-rectangle", "round")
        node_divs.append(f'''
        <div class="node {shape_cls}" style="left:{n['x']}px;top:{n['y']}px;width:{_SHAPE_W}px;">
          <div class="node-label">{html.escape(n['label'])}</div>
          {'<ul class="rules">'+rules_html+'</ul>' if rules_html else '<div class="no-rules">terminal action</div>' if n['terminal'] else '<div class="no-rules">(no rules)</div>'}
        </div>''')

    n_rules = sum(len(n["rules"]) for n in nodes)
    n_dia = sum(1 for n in nodes if n["shape"] == "diamond")
    return f'''<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(title)} — generated workflow</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1420;color:#e6ecf5;margin:0;padding:24px}}
 h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#9fb0c8;font-size:13px;margin-bottom:18px}}
 .stats span{{background:#1b2333;border:1px solid #2c3650;border-radius:6px;padding:4px 10px;margin-right:8px;font-size:12px}}
 .canvas{{position:relative;margin-top:20px;width:{width}px;height:{height}px}}
 svg{{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none}}
 .node{{position:absolute;background:#1b2333;border:1px solid #38456a;border-radius:10px;padding:10px 12px;box-shadow:0 4px 12px rgba(0,0,0,.35)}}
 .node.diamond{{border-color:#d98a3d;background:#241d12}}
 .node.round{{border-color:#3dbb7a;background:#10241a;border-radius:24px}}
 .node-label{{font-weight:600;font-size:13px;margin-bottom:6px;line-height:1.3}}
 ul.rules{{margin:6px 0 0;padding-left:16px;font-size:11.5px;color:#cdd9ec}} ul.rules li{{margin-bottom:5px;line-height:1.35}}
 .no-rules{{font-size:11px;color:#7e8aa3;font-style:italic}}
 .dt{{font-weight:700;font-size:10px;padding:1px 5px;border-radius:4px;margin-right:4px}}
 .dt-deny{{background:#5a1f24;color:#ff9aa2}} .dt-allow{{background:#173d2a;color:#7ee2a8}}
 .dt-bypass{{background:#1d3350;color:#8cc1ff}} .dt-pend{{background:#4a3a14;color:#ffd479}}
 .dt-refer{{background:#3a2350;color:#cBa3ff}} .dt-stop{{background:#5a1f24;color:#ff9aa2}}
 .dt-system,.dt-conditional,.dt-na,.dt-waive,.dt-override{{background:#23304a;color:#aebfe0}}
 .code{{color:#ffcf7a}} .goto{{color:#7ee2a8}} b{{color:#9fb0c8}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="sub">Generated by the dedicated PDF agent army (pdf_enrich) — no manual editing.</div>
<div class="stats">
 <span>{len(nodes)} nodes</span><span>{n_dia} decision diamonds</span>
 <span>{n_rules} rules</span><span>{len(state.get('steps') or [])} steps</span>
</div>
<div class="canvas">
 <svg><defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
   <path d="M0,0 L8,3 L0,6 Z" fill="#7c8aa5"/></marker></defs>{''.join(svg_lines)}</svg>
 {''.join(node_divs)}
</div></body></html>'''


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "OBH_Facets_Timely_Filing.pdf"
    print(f"Running PDF army on: {path}")
    st = run(path)
    nodes, edges = to_nodes(st)
    out_html = Path("scripts/pdf_workflow_preview.html")
    out_html.write_text(render_html(st, nodes, edges))
    out_json = Path("scripts/pdf_workflow_steps.json")
    out_json.write_text(json.dumps(st.get("steps") or [], indent=2))
    print("\n=== GENERATED WORKFLOW ===")
    for n in nodes:
        print(f"[{n['shape']:>15}] {n['label'][:80]}  ({len(n['rules'])} rules)")
    print(f"\nNodes: {len(nodes)}  Edges: {len(edges)}  "
          f"Rules: {sum(len(n['rules']) for n in nodes)}")
    print(f"HTML : {out_html.resolve()}")
    print(f"JSON : {out_json.resolve()}")
