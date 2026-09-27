"""
Tests for the database-level audit trigger.

The property under test: a status cannot change in the database without an
audit row appearing, even if the change bypasses the application entirely.
That is what makes the audit log trustworthy when the operator asks
"who moved this card" — including for changes made by hand from psql.
"""

import pytest
from django.db import connection

from board.models import Status, Task

pytestmark = pytest.mark.django_db


def _trigger_installed() -> bool:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_trigger WHERE tgname = 'trg_task_audit' AND NOT tgisinternal"
        )
        return cur.fetchone() is not None


def test_audit_trigger_is_installed():
    assert _trigger_installed(), "migration 0002 did not run against this database"


def test_raw_sql_status_change_is_logged():
    t = Task.objects.create(title="raw", acceptance="x", status=Status.READY)
    before = t.events.count()

    with connection.cursor() as cur:
        cur.execute("UPDATE tasks SET status = %s WHERE id = %s", [Status.REVIEW, t.pk])

    t.refresh_from_db()
    assert t.status == Status.REVIEW
    assert t.events.count() > before

    ev = t.events.filter(event="db_trigger").first()
    assert ev is not None
    assert ev.from_status == Status.READY
    assert ev.to_status == Status.REVIEW


def test_trigger_ignores_non_status_updates():
    """Only status changes are audited — heartbeat noise must not flood the log."""
    t = Task.objects.create(title="hb", acceptance="x", status=Status.READY)
    before = t.events.count()

    with connection.cursor() as cur:
        cur.execute("UPDATE tasks SET current_step = %s WHERE id = %s", ["working", t.pk])

    t.refresh_from_db()
    assert t.current_step == "working"
    assert t.events.count() == before
    assert t.events.filter(event="db_trigger").count() == 0


def test_application_transition_produces_both_rows():
    """
    Explicit event from the application plus the trigger row. Duplication is
    intentional: the trigger row is the tamper-evident floor, the application
    row carries the actor and reason.
    """
    from board.models import Actor
    from board.state import Ctx, transition

    t = Task.objects.create(title="both", acceptance="x", status=Status.READY)
    transition(t, Status.IN_PROGRESS, Ctx(actor=Actor.AGENT))

    events = list(t.events.values_list("event", flat=True))
    assert "claim" in events or "transition" in events
    assert "db_trigger" in events
