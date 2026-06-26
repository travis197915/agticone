"""Data migration: populate the server-driven builder catalog.

Creates the canonical shape palette, sidebar nav, and dashboard widgets that the
frontend and the SOP auto-build path (``builder/sop_autobuild.py``) depend on.
This is the data-migration replacement for the old seed command/signal: it runs
as part of ``migrate``, is idempotent (``update_or_create`` by slug), and never
deletes user-placed shapes.
"""
from __future__ import annotations

from django.db import migrations

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

_DECISION_SCHEMA = [
    {"name": "label",    "label": "Condition",  "type": "string"},
    {"name": "yesLabel", "label": "Yes branch", "type": "string"},
    {"name": "noLabel",  "label": "No branch",  "type": "string"},
]


def _palette(fill, stroke, color, accent):
    return {"fill": fill, "stroke": stroke, "color": color, "accent": accent}


# (slug, label, kind, w, h, node_label, style, property_schema, order)
GENERAL_SHAPES = [
    ("round-rectangle", "Terminator",         "terminator",
        160, 56, "Start",
        _palette("#ecfdf5", "#10b981", "#065f46", "#10b981"),
        _TEXT_LABEL_SCHEMA, 10),
    ("rectangle",       "Process",            "process",
        160, 56, "Process",
        _palette("#eff6ff", "#3b82f6", "#1e3a8a", "#3b82f6"),
        _TEXT_LABEL_SCHEMA, 20),
    ("diamond",         "Decision",           "decision",
        160, 56, "Decision",
        _palette("#fffbeb", "#f59e0b", "#78350f", "#f59e0b"),
        _DECISION_SCHEMA, 30),
    ("parallelogram",   "Data",               "data",
        160, 56, "Data",
        _palette("#f5f3ff", "#8b5cf6", "#4c1d95", "#8b5cf6"),
        _TEXT_LABEL_SCHEMA, 40),
    ("hexagon",         "Preparation",        "preparation",
        160, 56, "Preparation",
        _palette("#fff7ed", "#f97316", "#7c2d12", "#f97316"),
        _TEXT_LABEL_SCHEMA, 50),
    ("cylinder",        "Database",           "database",
        160, 56, "Database",
        _palette("#f0fdfa", "#14b8a6", "#134e4a", "#14b8a6"),
        _TEXT_LABEL_SCHEMA, 60),
    ("circle",          "Connector",          "connector",
        140, 56, "Connector",
        _palette("#f1f5f9", "#64748b", "#1e293b", "#64748b"),
        _TEXT_LABEL_SCHEMA, 70),
    ("arrow-rectangle", "Predefined Process", "subprocess",
        160, 56, "Subprocess",
        _palette("#eef2ff", "#6366f1", "#312e81", "#6366f1"),
        _TEXT_LABEL_SCHEMA, 80),
]

# (section, slug, label, icon, href, min_role, order)
SIDEBAR_NAV = [
    ("",              "dashboard",      "Dashboard",      "LayoutDashboard", "/dashboard",             "MEMBER", 10),
    ("Automation",    "agents",         "Agents",         "Bot",             "/agents",                "MEMBER", 20),
    ("Automation",    "workflows",      "Workflows",      "GitBranch",       "/workflows",             "MEMBER", 30),
    ("Usage & Cost",  "activity",       "Activity",       "Activity",        "/activity",              "MEMBER", 40),
    ("Usage & Cost",  "ai-usage",       "AI Usage",       "Sparkles",        "/ai-usage",              "MEMBER", 50),
    ("Configuration", "field-mapping",  "Field Mapping",  "Table2",          "/config/field-mapping",  "MEMBER", 54),
    ("Configuration", "claim-ontology", "Claim Ontology", "ListTree",        "/config/claim-ontology", "MEMBER", 56),
    ("Account",       "users",          "Users",          "Users",           "/users",                 "ADMIN",  60),
    ("Account",       "settings",       "Settings",       "Settings",        "/settings",              "MEMBER", 70),
]

# (slug, label, kind, icon, value, query, color, order)
DASHBOARD_WIDGETS = [
    ("total-agents",     "Total Agents",     "stat", "Bot",         "0", "",                                       "amber",   10),
    ("online-agents",    "Online Agents",    "stat", "Activity",    "0", "",                                       "emerald", 20),
    ("active-workflows", "Active Workflows", "stat", "GitBranch",   "0", "/api/builder/workflows/?is_active=true", "violet",  30),
    ("total-runs",       "Total Runs",       "stat", "Clock",       "0", "",                                       "sky",     40),
    ("days-renewal",     "Days till Renewal","stat", "ShieldCheck", "—", "",                                       "rose",    50),
]


def seed_catalog(apps, schema_editor):
    ShapeCategory = apps.get_model("builder", "ShapeCategory")
    ShapeDefinition = apps.get_model("builder", "ShapeDefinition")
    NavItem = apps.get_model("builder", "NavItem")
    DashboardWidget = apps.get_model("builder", "DashboardWidget")

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

    for (slug, label, kind, w, h, node_label,
         style, property_schema, order) in GENERAL_SHAPES:
        ShapeDefinition.objects.update_or_create(
            slug=slug,
            defaults=dict(
                category=general,
                label=label,
                kind=kind,
                viewbox="",
                svg_path="",
                default_width=w,
                default_height=h,
                default_label=node_label,
                ports=_FOUR_PORTS,
                property_schema=property_schema,
                default_style=style,
                order=order,
                is_active=True,
            ),
        )

    for section, slug, label, icon, href, min_role, order in SIDEBAR_NAV:
        NavItem.objects.update_or_create(
            slug=slug,
            defaults=dict(
                label=label, icon=icon, href=href,
                section=section, min_role=min_role,
                order=order, is_active=True,
            ),
        )

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


def unseed_catalog(apps, schema_editor):
    # Reverse: remove only the canonical catalog rows this migration created,
    # leaving any user-placed shapes/workflows untouched.
    ShapeDefinition = apps.get_model("builder", "ShapeDefinition")
    NavItem = apps.get_model("builder", "NavItem")
    DashboardWidget = apps.get_model("builder", "DashboardWidget")
    ShapeCategory = apps.get_model("builder", "ShapeCategory")

    NavItem.objects.filter(slug__in=[r[1] for r in SIDEBAR_NAV]).delete()
    DashboardWidget.objects.filter(slug__in=[r[0] for r in DASHBOARD_WIDGETS]).delete()
    # Skip any definition still referenced by a placed Shape (PROTECT FK).
    ShapeDefinition.objects.filter(
        slug__in=[r[0] for r in GENERAL_SHAPES]
    ).exclude(instances__isnull=False).delete()
    ShapeCategory.objects.filter(slug="general").exclude(
        shapes__isnull=False
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("builder", "0002_alter_shape_label"),
    ]

    operations = [
        migrations.RunPython(seed_catalog, unseed_catalog),
    ]
