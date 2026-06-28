"""Add the MCP Servers + Tool Calls config screens to the sidebar nav.

Mirrors the Field Mapping / Claim Ontology rows seeded in ``0003_seed_catalog``
so the new DB-backed MCP/tool-call config pages surface under "Configuration".
"""
from django.db import migrations

# (section, slug, label, icon, href, min_role, order)
NAV = [
    ("Configuration", "mcp-servers", "MCP Servers", "Server", "/config/mcp-servers", "MEMBER", 57),
    ("Configuration", "tool-calls",  "Tool Calls",  "Wrench", "/config/tool-calls",  "MEMBER", 58),
]


def seed_nav(apps, schema_editor):
    NavItem = apps.get_model("builder", "NavItem")
    for section, slug, label, icon, href, min_role, order in NAV:
        NavItem.objects.update_or_create(
            slug=slug,
            defaults=dict(
                label=label, icon=icon, href=href,
                section=section, min_role=min_role,
                order=order, is_active=True,
            ),
        )


def unseed_nav(apps, schema_editor):
    NavItem = apps.get_model("builder", "NavItem")
    NavItem.objects.filter(slug__in=[r[1] for r in NAV]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("builder", "0003_seed_catalog"),
    ]

    operations = [
        migrations.RunPython(seed_nav, unseed_nav),
    ]
