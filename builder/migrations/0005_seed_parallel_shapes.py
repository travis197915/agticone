"""Data migration: seed the parallel-execution control shapes.

Adds two structural ShapeDefinitions used when a workflow runs in *parallel*
mode (see ``builder/views.py`` ``execution_mode`` action):

  * ``parallel-fork`` — a split/fan-out node placed before all SOP columns.
  * ``verdict``       — a join/aggregate node placed after all SOP columns; it
                        represents the fused final adjudication.

Both carry no rule bindings, so the execution engine's rule loader ignores
them — they exist purely to make the parallel topology visible on the canvas.
Idempotent (``update_or_create`` by slug); reversible.
"""
from __future__ import annotations

from django.db import migrations

_FOUR_PORTS = [
    {"id": "top",    "x": 0.5, "y": 0.0, "side": "top",    "kind": "both"},
    {"id": "right",  "x": 1.0, "y": 0.5, "side": "right",  "kind": "both"},
    {"id": "bottom", "x": 0.5, "y": 1.0, "side": "bottom", "kind": "both"},
    {"id": "left",   "x": 0.0, "y": 0.5, "side": "left",   "kind": "both"},
]

_LABEL_SCHEMA = [
    {"name": "label", "label": "Label", "type": "string"},
    {"name": "description", "label": "Description", "type": "text"},
]

# (slug, label, kind, w, h, node_label, style, order)
CONTROL_SHAPES = [
    ("parallel-fork", "Parallel Split", "fork",
        180, 60, "Parallel Split",
        {"fill": "#eef2ff", "stroke": "#6366f1", "color": "#312e81", "accent": "#6366f1"},
        10),
    ("verdict", "Verdict", "verdict",
        200, 64, "Verdict",
        {"fill": "#ecfdf5", "stroke": "#10b981", "color": "#065f46", "accent": "#10b981"},
        20),
]


def seed(apps, schema_editor):
    ShapeCategory = apps.get_model("builder", "ShapeCategory")
    ShapeDefinition = apps.get_model("builder", "ShapeDefinition")

    control, _ = ShapeCategory.objects.update_or_create(
        slug="control",
        defaults=dict(
            label="Control Flow",
            description="Structural nodes that shape how SOPs run — parallel "
                        "split (fan-out) and verdict (fan-in / aggregate).",
            order=20,
            is_active=True,
        ),
    )

    for slug, label, kind, w, h, node_label, style, order in CONTROL_SHAPES:
        ShapeDefinition.objects.update_or_create(
            slug=slug,
            defaults=dict(
                category=control,
                label=label,
                kind=kind,
                viewbox="",
                svg_path="",
                default_width=w,
                default_height=h,
                default_label=node_label,
                ports=_FOUR_PORTS,
                property_schema=_LABEL_SCHEMA,
                default_style=style,
                order=order,
                is_active=True,
            ),
        )


def unseed(apps, schema_editor):
    ShapeDefinition = apps.get_model("builder", "ShapeDefinition")
    ShapeCategory = apps.get_model("builder", "ShapeCategory")
    # Skip any definition still referenced by a placed Shape (PROTECT FK).
    ShapeDefinition.objects.filter(
        slug__in=[r[0] for r in CONTROL_SHAPES]
    ).exclude(instances__isnull=False).delete()
    ShapeCategory.objects.filter(slug="control").exclude(
        shapes__isnull=False
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("builder", "0004_seed_mcp_nav"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
