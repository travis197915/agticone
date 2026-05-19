"""
Seed data for the server-driven palette + sidebar + dashboard.

Idempotent: :func:`seed_all` upserts by slug and prunes any catalog entry
that's no longer in :data:`GENERAL_SHAPES`.  Runs from
`manage.py seed_builder_catalog` and a post-migrate signal.

We deliberately keep the palette small — eight canonical flow-chart shapes
(terminator, process, decision, data, document, database, cloud, connector)
matching the BS / ISO 5807 set.  Each shape has four edge ports
(`top`, `right`, `bottom`, `left`); SVG paths use a normalised 100×100
viewbox so the renderer can stretch them to any width/height.
"""
from __future__ import annotations

from typing import Iterable

from django.db import transaction


# ── Default four-port set used by most shapes ────────────────────────────────


_FOUR_PORTS = [
    {"id": "top",    "x": 0.5, "y": 0.0, "side": "top",    "kind": "both"},
    {"id": "right",  "x": 1.0, "y": 0.5, "side": "right",  "kind": "both"},
    {"id": "bottom", "x": 0.5, "y": 1.0, "side": "bottom", "kind": "both"},
    {"id": "left",   "x": 0.0, "y": 0.5, "side": "left",   "kind": "both"},
]

_TEXT_LABEL_SCHEMA = [
    {"name": "label", "label": "Label", "type": "string"},
    {"name": "description", "label": "Description", "type": "text"},
]


# ── Shape definitions ────────────────────────────────────────────────────────


_DECISION_SCHEMA = [
    {"name": "label",     "label": "Condition",  "type": "string"},
    {"name": "yesLabel",  "label": "Yes branch", "type": "string"},
    {"name": "noLabel",   "label": "No branch",  "type": "string"},
]


# The canvas uses React Flow's standard rectangular node (no custom SVG —
# `@xyflow/react` doesn't ship shape components, only `input`/`default`/
# `output` rectangles).  We distinguish shapes by *behavior* instead of
# silhouette: each catalog entry has its own colour palette + label.
#
# `default_style` shape:
#   { fill, stroke, color, accent }   — all CSS colour strings.
#   `fill`   background of the rectangle.
#   `stroke` 2-px border.
#   `color`  label text.
#   `accent` small left-edge stripe that signals the role at a glance.
#
# columns: (slug, flowchart_label, kind,
#           default_w, default_h, default_node_label,
#           default_style, property_schema, order)
def _palette(fill: str, stroke: str, color: str, accent: str) -> dict:
    return {"fill": fill, "stroke": stroke, "color": color, "accent": accent}


GENERAL_SHAPES_V2 = [
    # role:  Start / End             (green)
    ("round-rectangle", "Terminator",         "terminator",
        160, 56, "Start",
        _palette("#ecfdf5", "#10b981", "#065f46", "#10b981"),
        _TEXT_LABEL_SCHEMA, 10),

    # role:  Process / Action         (blue)
    ("rectangle",       "Process",            "process",
        160, 56, "Process",
        _palette("#eff6ff", "#3b82f6", "#1e3a8a", "#3b82f6"),
        _TEXT_LABEL_SCHEMA, 20),

    # role:  Branch / Conditional     (amber)
    ("diamond",         "Decision",           "decision",
        160, 56, "Decision",
        _palette("#fffbeb", "#f59e0b", "#78350f", "#f59e0b"),
        _DECISION_SCHEMA, 30),

    # role:  Input / Output           (violet)
    ("parallelogram",   "Data",               "data",
        160, 56, "Data",
        _palette("#f5f3ff", "#8b5cf6", "#4c1d95", "#8b5cf6"),
        _TEXT_LABEL_SCHEMA, 40),

    # role:  Setup / Init             (orange)
    ("hexagon",         "Preparation",        "preparation",
        160, 56, "Preparation",
        _palette("#fff7ed", "#f97316", "#7c2d12", "#f97316"),
        _TEXT_LABEL_SCHEMA, 50),

    # role:  Persistent storage       (teal)
    ("cylinder",        "Database",           "database",
        160, 56, "Database",
        _palette("#f0fdfa", "#14b8a6", "#134e4a", "#14b8a6"),
        _TEXT_LABEL_SCHEMA, 60),

    # role:  Reference / Connector    (slate)
    ("circle",          "Connector",          "connector",
        140, 56, "Connector",
        _palette("#f1f5f9", "#64748b", "#1e293b", "#64748b"),
        _TEXT_LABEL_SCHEMA, 70),

    # role:  Subprocess / Predefined  (indigo)
    ("arrow-rectangle", "Predefined Process", "subprocess",
        160, 56, "Subprocess",
        _palette("#eef2ff", "#6366f1", "#312e81", "#6366f1"),
        _TEXT_LABEL_SCHEMA, 80),
]


# Legacy → canonical slug remap.  Applied before pruning so existing rows
# in `builder_shape` survive the schema cleanup with their visuals intact.
LEGACY_SLUG_REMAP = {
    "rect-rounded": "round-rectangle",
    "rect":         "rectangle",
    "ellipse":      "circle",
    "document":     "parallelogram",
    "cloud":        "parallelogram",
}


# ── Sidebar navigation ────────────────────────────────────────────────────────


SIDEBAR_NAV = [
    # section          slug             label          icon          href           min_role  order
    ("",               "dashboard",     "Dashboard",   "LayoutDashboard", "/dashboard", "MEMBER", 10),
    ("Automation",     "agents",        "Agents",      "Bot",             "/agents",    "MEMBER", 20),
    ("Automation",     "workflows",     "Workflows",   "GitBranch",       "/workflows", "MEMBER", 30),
    ("Usage & Cost",   "activity",      "Activity",    "Activity",        "/activity",  "MEMBER", 40),
    ("Usage & Cost",   "ai-usage",      "AI Usage",    "Sparkles",        "/ai-usage",  "MEMBER", 50),
    ("Account",        "users",         "Users",       "Users",           "/users",     "ADMIN",  60),
    ("Account",        "settings",      "Settings",    "Settings",        "/settings",  "MEMBER", 70),
]


# ── Dashboard widgets ────────────────────────────────────────────────────────


DASHBOARD_WIDGETS = [
    # slug              label                kind    icon         value  query                                   color           order
    ("total-agents",    "Total Agents",      "stat", "Bot",         "0",  "",                                     "amber",        10),
    ("online-agents",   "Online Agents",     "stat", "Activity",    "0",  "",                                     "emerald",      20),
    ("active-workflows","Active Workflows",  "stat", "GitBranch",   "0",  "/api/builder/workflows/?is_active=true","violet",       30),
    ("total-runs",      "Total Runs",        "stat", "Clock",       "0",  "",                                     "sky",          40),
    ("days-renewal",    "Days till Renewal", "stat", "ShieldCheck", "—",  "",                                     "rose",         50),
]


# ── Idempotent upsert ────────────────────────────────────────────────────────


@transaction.atomic
def seed_all() -> dict:
    """Idempotently upsert the catalog.  Returns row counts for diagnostics."""

    from .models import (  # local import to avoid circular at app-load
        DashboardWidget,
        NavItem,
        Shape,
        ShapeCategory,
        ShapeDefinition,
    )

    general, _ = ShapeCategory.objects.update_or_create(
        slug="general",
        defaults=dict(
            label="General",
            description="Standard flow-chart primitives — terminator, process, "
            "decision, data, database, connector, and friends.",
            order=10,
            is_active=True,
        ),
    )

    # 1. Upsert the canonical eight first so the remap step has valid targets.
    keep_slugs = {row[0] for row in GENERAL_SHAPES_V2}
    n_shapes = 0
    for (
        slug, label, kind,
        default_w, default_h, default_label,
        style, property_schema, order,
    ) in GENERAL_SHAPES_V2:
        ShapeDefinition.objects.update_or_create(
            slug=slug,
            defaults=dict(
                category=general,
                label=label,
                kind=kind,
                viewbox="",     # legacy field, unused since we dropped SVG paths
                svg_path="",    # ditto
                default_width=default_w,
                default_height=default_h,
                default_label=default_label,
                ports=_FOUR_PORTS,
                property_schema=property_schema,
                default_style=style,
                order=order,
                is_active=True,
            ),
        )
        n_shapes += 1

    # 2. Remap placed Shapes whose definition uses a legacy slug.
    remapped = 0
    for old_slug, new_slug in LEGACY_SLUG_REMAP.items():
        old = ShapeDefinition.objects.filter(slug=old_slug).first()
        new = ShapeDefinition.objects.filter(slug=new_slug).first()
        if not old or not new or old.pk == new.pk:
            continue
        remapped += Shape.objects.filter(definition=old).update(definition=new)

    # 3. Now safe to prune obsolete definitions (PROTECT FK would block if a
    #    Shape still pointed at one — the remap above clears that path).
    pruned = ShapeDefinition.objects.exclude(slug__in=keep_slugs).delete()[0]

    n_nav = 0
    for section, slug, label, icon, href, min_role, order in SIDEBAR_NAV:
        NavItem.objects.update_or_create(
            slug=slug,
            defaults=dict(
                label=label, icon=icon, href=href,
                section=section, min_role=min_role,
                order=order, is_active=True,
            ),
        )
        n_nav += 1

    n_widgets = 0
    for slug, label, kind, icon, value, query, color, order in DASHBOARD_WIDGETS:
        DashboardWidget.objects.update_or_create(
            slug=slug,
            defaults=dict(
                label=label, kind=kind, icon=icon,
                value=value, query=query,
                color_class=color, order=order,
                is_active=True,
            ),
        )
        n_widgets += 1

    return {
        "shapes": n_shapes,
        "remapped": remapped,
        "pruned": pruned,
        "nav": n_nav,
        "widgets": n_widgets,
    }


def yield_seed_summary() -> Iterable[str]:  # pragma: no cover - CLI helper
    counts = seed_all()
    for key, n in counts.items():
        yield f"upserted {n:>3} {key}"
