# Adds the "html_block" choice to SopExclusion.target_kind so users can
# exclude arbitrary HTML sections (extracted server-side from the raw SOP).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sop_ingestion", "0009_sop_exclusion"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sopexclusion",
            name="target_kind",
            field=models.CharField(
                choices=[
                    ("rule",       "Rule"),
                    ("step",       "Step"),
                    ("section",    "Pre-condition section"),
                    ("sop",        "Whole SOP"),
                    ("graph_node", "Graph node"),
                    ("html_block", "Raw HTML block"),
                ],
                default="rule",
                max_length=16,
            ),
        ),
    ]
