"""
Alter the foreign key on ``execution_rule_evaluation.rule_binding_id`` from the
default RESTRICT behaviour to ON DELETE SET NULL.

The column is already nullable (was designed for this semantics), but the
constraint was created without an explicit ON DELETE clause, which Postgres
defaults to RESTRICT.  This caused an IntegrityError whenever the canvas PUT
tried to delete NodeRuleBinding rows that were still referenced by execution
history rows.
"""
from django.db import migrations

_CONSTRAINT = "execution_rule_evalu_rule_binding_id_441cc904_fk_node_rule"


class Migration(migrations.Migration):

    dependencies = [
        ("agent_tools", "0004_move_to_agent_tools_schema"),
    ]

    operations = [
        migrations.RunSQL(
            sql=f"""
                ALTER TABLE execution_rule_evaluation
                DROP CONSTRAINT IF EXISTS "{_CONSTRAINT}";

                ALTER TABLE execution_rule_evaluation
                ADD CONSTRAINT "{_CONSTRAINT}"
                FOREIGN KEY (rule_binding_id)
                REFERENCES node_rule_binding(id)
                ON DELETE SET NULL
                DEFERRABLE INITIALLY DEFERRED;
            """,
            reverse_sql=f"""
                ALTER TABLE execution_rule_evaluation
                DROP CONSTRAINT IF EXISTS "{_CONSTRAINT}";

                ALTER TABLE execution_rule_evaluation
                ADD CONSTRAINT "{_CONSTRAINT}"
                FOREIGN KEY (rule_binding_id)
                REFERENCES node_rule_binding(id);
            """,
            hints={"target_schema": "public"},
        ),
    ]
