"""
Tests for the project shell: settings, entrypoints, migration reversibility,
and defensive branches in the sweeper.

These are the bits nobody thinks about until deployment goes wrong.
"""

import os

import pytest
from django.db import connection

from board.models import Status, Task
from board.state import Ctx, TransitionError, transition, unblock_ready

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def test_env_bool_true_values():
    from core.settings import env_bool

    for raw in ("1", "true", "TRUE", "yes", "on", " on "):
        os.environ["_TEST_BOOL"] = raw
        assert env_bool("_TEST_BOOL", default=False) is True, raw


def test_env_bool_false_and_default():
    from core.settings import env_bool

    os.environ.pop("_TEST_BOOL", None)
    assert env_bool("_TEST_BOOL", default=False) is False
    assert env_bool("_TEST_BOOL", default=True) is True

    for raw in ("0", "false", "no", "off", "banana"):
        os.environ["_TEST_BOOL"] = raw
        assert env_bool("_TEST_BOOL", default=True) is False, raw


def test_env_returns_default_when_unset():
    from core.settings import env

    os.environ.pop("_TEST_ABSENT", None)
    assert env("_TEST_ABSENT") is None
    assert env("_TEST_ABSENT", "fallback") == "fallback"


def test_runtime_tuning_defaults_are_sane():
    from django.conf import settings

    # a lease must outlive several heartbeats, otherwise healthy work looks dead
    assert settings.TASK_LEASE_SECONDS > settings.TASK_HEARTBEAT_SECONDS
    assert settings.TASK_DEFAULT_MAX_ATTEMPTS >= 1
    assert settings.TASK_DEFAULT_MAX_TOKENS > 0
    assert 1 <= settings.MAX_DEPTH <= 6


# --------------------------------------------------------------------------
# entrypoints
# --------------------------------------------------------------------------


def test_wsgi_application_is_importable():
    from core.wsgi import application

    assert callable(application)


def test_asgi_application_is_importable():
    from core.asgi import application

    assert callable(application)


def test_healthz_view_resolves():
    from django.urls import resolve

    match = resolve("/healthz")
    assert match.func is not None


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------


def test_audit_migration_is_reversible():
    """
    The 0002 down path must drop the trigger. If it did not, a rollback would
    leave an orphaned trigger firing against a schema that no longer guarantees
    the columns it references.
    """
    from importlib import import_module

    mod = import_module("board.migrations.0002_audit_trigger")
    assert "DROP TRIGGER" in mod.REVERSE_SQL
    # Django names the FK column task_id, not task — getting this wrong made
    # the trigger error on every single status change.
    assert "task_id" in mod.TRIGGER_SQL
    assert "IS DISTINCT FROM" in mod.TRIGGER_SQL


def test_audit_trigger_tolerates_non_status_update():
    t = Task.objects.create(title="x", acceptance="a", status=Status.READY)
    before = t.events.count()
    with connection.cursor() as cur:
        cur.execute("UPDATE tasks SET attempts = attempts + 1 WHERE id = %s", [t.pk])
    assert t.events.count() == before


# --------------------------------------------------------------------------
# defensive branch in unblock_ready
# --------------------------------------------------------------------------


def test_unblock_ready_survives_a_failing_transition(monkeypatch):
    """
    unblock_ready checks dependencies and then calls transition, which re-checks
    them. If the two ever disagree (a concurrent change, a future extra guard)
    the loop must skip that card instead of dying and stalling every other card.
    """
    from board.models import TaskDep
    from board import state

    blocker = Task.objects.create(
        title="blocker", acceptance="x", status=Status.IN_PROGRESS
    )
    blocked = Task.objects.create(
        title="blocked", acceptance="x", status=Status.BLOCKED
    )
    other = Task.objects.create(
        title="other", acceptance="x", status=Status.BLOCKED
    )
    TaskDep.objects.create(task=blocked, depends_on=blocker)
    TaskDep.objects.create(task=other, depends_on=blocker)
    blocker.status = Status.DONE
    blocker.save()

    real_transition = state.transition
    calls = {"n": 0}

    def flaky(task, to, ctx=None, event="transition"):
        if task.pk == blocked.pk:
            raise TransitionError("simulated race")
        return real_transition(task, to, ctx, event)

    monkeypatch.setattr(state, "transition", flaky)

    assert unblock_ready() == 1
    blocked.refresh_from_db()
    other.refresh_from_db()
    assert blocked.status == Status.BLOCKED
    assert other.status == Status.READY


# --------------------------------------------------------------------------
# licensing invariants
# --------------------------------------------------------------------------


def test_license_states_no_free_use():
    """
    The whole point of the licence is that public visibility confers nothing.
    If someone edits that sentence away, the repository is quietly a free
    project. This fails loudly instead.
    """
    from pathlib import Path

    text = " ".join((Path(__file__).resolve().parents[2] / "LICENSE").read_text().split())
    assert "Copyright (c) 2026 Kuduza Ai Lab" in text
    assert "NO FREE USE IS GRANTED" in text
    assert "not an open-source license" in text.lower()


def test_license_reserves_third_party_material():
    """
    The rights holder grants nothing over anyone else's code. If third-party
    material were ever covered by the proprietary grant, that would be a
    licence violation by the project itself.

    Whitespace is normalised first: the licence is hard-wrapped for
    readability, so a clause can legitimately break across lines without its
    wording having changed.
    """
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "LICENSE").read_text()
    flat = " ".join(text.split())
    assert "THIRD-PARTY MATERIAL" in flat
    assert "grants You NO right, license, permission" in flat
    assert "the third-party licence prevails" in flat
    # the definition must be referenced from the definitions section
    assert "has the meaning given in Section 10.2" in flat


def test_notice_states_authorship_and_provenance():
    from pathlib import Path

    text = " ".join((Path(__file__).resolve().parents[2] / "NOTICE").read_text().split())
    assert "Mavis Agent (MiniMax M3.1 powered)" in text
    assert "under the supervision and direction" in text
    assert "Kuduza Ai Lab" in text
    assert "NOT to grant free use" in text
