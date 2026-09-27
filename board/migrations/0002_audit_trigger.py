"""
Database-level audit trigger.

The application already writes TaskEvent rows explicitly. This trigger is the
backstop: a status can never change in the database without an audit row
existing, even if some future code path forgets to log, or someone runs a
manual UPDATE from psql.

That property is the whole point of an audit log that an operator is supposed
to trust when they ask "who moved this card".
"""

from django.db import migrations

TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION board_audit_task_change()
RETURNS trigger AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO task_events (task_id, ts, actor, event, from_status, to_status, payload)
        VALUES (
            NEW.id,
            now(),
            'system',
            'db_trigger',
            OLD.status,
            NEW.status,
            jsonb_build_object(
                'actor_col', COALESCE(NEW.claimed_by, ''),
                'attempts', NEW.attempts,
                'stuck_score', NEW.stuck_score
            )
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_task_audit ON tasks;
CREATE TRIGGER trg_task_audit
AFTER UPDATE ON tasks
FOR EACH ROW
EXECUTE FUNCTION board_audit_task_change();
"""

REVERSE_SQL = "DROP TRIGGER IF EXISTS trg_task_audit ON tasks;"


def apply_trigger(apps, schema_editor):
    schema_editor.execute(TRIGGER_SQL)


def drop_trigger(apps, schema_editor):
    schema_editor.execute(REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [("board", "0001_initial")]

    operations = [
        migrations.RunPython(apply_trigger, drop_trigger),
    ]
