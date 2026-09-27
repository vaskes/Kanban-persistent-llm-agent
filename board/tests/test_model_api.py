"""
Tests for the model API surface: __str__, derived properties, querysets.

Small units, but they are the properties the board template and the runtime
both read, and an untested property is a property nobody can rely on.
"""

import pytest
from django.utils import timezone

from board.models import (
    Actor,
    Attempt,
    Memory,
    Status,
    Task,
    TaskDep,
    TaskEvent,
)

pytestmark = pytest.mark.django_db


def mk_task(**kw) -> Task:
    defaults = dict(title="card", acceptance="x", status=Status.READY)
    defaults.update(kw)
    return Task.objects.create(**defaults)


# --------------------------------------------------------------------------
# __str__
# --------------------------------------------------------------------------


def test_task_str():
    t = mk_task(title="a" * 100)
    s = str(t)
    assert s.startswith("[READY]")
    assert len(s) < 80  # title is truncated to 60


def test_task_dep_str():
    a = mk_task(title="a")
    b = mk_task(title="b")
    d = TaskDep.objects.create(task=b, depends_on=a)
    assert str(d) == f"{b.pk} <- {a.pk}"


def test_task_event_str_with_task():
    t = mk_task()
    e = TaskEvent.objects.create(task=t, actor=Actor.AGENT, event="claim")
    assert t.pk in str(e)
    assert "agent/claim" in str(e)


def test_task_event_str_without_task():
    e = TaskEvent.objects.create(task=None, actor=Actor.SYSTEM, event="swept")
    assert "-" in str(e)


def test_attempt_str_running():
    t = mk_task()
    a = Attempt.objects.create(task=t)
    assert "running" in str(a)


def test_attempt_str_with_outcome():
    t = mk_task()
    a = Attempt.objects.create(task=t, outcome="success")
    assert "success" in str(a)


def test_memory_str():
    m = Memory.objects.create(kind="gotcha", content="g" * 100)
    assert str(m).startswith("gotcha:")
    assert len(str(m)) < 70


# --------------------------------------------------------------------------
# budget / lease properties
# --------------------------------------------------------------------------


def test_attempts_exhausted():
    assert mk_task(attempts=3, max_attempts=3).attempts_exhausted is True
    assert mk_task(attempts=2, max_attempts=3).attempts_exhausted is False


def test_tokens_exhausted():
    assert mk_task(tokens_used=100, max_tokens=100).tokens_exhausted is True
    assert mk_task(tokens_used=99, max_tokens=100).tokens_exhausted is False


def test_budget_exhausted_either_way():
    assert mk_task(attempts=9, max_attempts=3).budget_exhausted is True
    assert mk_task(tokens_used=900, max_tokens=100).budget_exhausted is True
    assert mk_task(attempts=0, tokens_used=0).budget_exhausted is False


def test_lease_expired():
    t = mk_task()
    assert t.lease_expired is False  # no lease at all
    t.lease_expires_at = timezone.now() - timezone.timedelta(minutes=1)
    assert t.lease_expired is True
    t.lease_expires_at = timezone.now() + timezone.timedelta(minutes=10)
    assert t.lease_expired is False


def test_has_evidence():
    assert mk_task(evidence={"exit_code": 0}).has_evidence() is True
    assert mk_task(evidence={}).has_evidence() is False


# --------------------------------------------------------------------------
# queryset helpers
# --------------------------------------------------------------------------


def test_queryset_ready():
    mk_task(title="r", status=Status.READY)
    mk_task(title="b", status=Status.BACKLOG)
    assert [t.title for t in Task.objects.ready()] == ["r"]


def test_queryset_active():
    mk_task(title="i", status=Status.IN_PROGRESS)
    mk_task(title="v", status=Status.REVIEW)
    mk_task(title="k", status=Status.BLOCKED)
    mk_task(title="d", status=Status.DONE)
    assert sorted(t.title for t in Task.objects.active()) == ["i", "k", "v"]


def test_unmet_dependencies_lists_open_edges():
    from board.state import Ctx, transition

    blocker = mk_task(title="blocker", status=Status.IN_PROGRESS)
    waiter = mk_task(title="waiter", status=Status.BACKLOG)
    TaskDep.objects.create(task=waiter, depends_on=blocker)

    assert waiter.unmet_dependencies() == [blocker.pk]

    transition(blocker, Status.REVIEW, Ctx(actor=Actor.AGENT, evidence={"ok": 1}))
    transition(blocker, Status.DONE, Ctx(actor=Actor.VERIFIER))
    assert waiter.unmet_dependencies() == []


def test_dependents_reverse_relation():
    a = mk_task(title="a")
    b = mk_task(title="b")
    TaskDep.objects.create(task=b, depends_on=a)
    assert [d.task_id for d in a.dependents.all()] == [b.pk]
