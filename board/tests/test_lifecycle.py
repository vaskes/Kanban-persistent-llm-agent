"""
Tests for the execution lifecycle: claim, lease, heartbeat, sweep.

This is the crash-recovery contract. If these pass, a worker that gets SIGKILLed
mid-task cannot strand a card.
"""

import pytest
from django.utils import timezone

from board.models import Actor, Attempt, Status, Task
from board.state import (
    Ctx,
    TransitionError,
    claim,
    heartbeat,
    sweep_leases,
    transition,
    unblock_ready,
)

pytestmark = pytest.mark.django_db


def make_task(**kw) -> Task:
    defaults = dict(
        title="card",
        acceptance="x",
        status=Status.READY,
        autonomy="AUTO",
    )
    defaults.update(kw)
    return Task.objects.create(**defaults)


# --------------------------------------------------------------------------
# claim
# --------------------------------------------------------------------------


def test_claim_takes_highest_priority_first():
    low = make_task(title="low", priority=2)
    high = make_task(title="high", priority=9)
    make_task(title="mid", priority=5)

    got = claim("w1", lease_seconds=900)
    assert got.pk == high.pk
    assert low.status == Status.READY


def test_claim_sets_lease_and_increments_attempts():
    t = make_task()
    got = claim("w1", lease_seconds=900)
    got.refresh_from_db()
    assert got.status == Status.IN_PROGRESS
    assert got.claimed_by == "w1"
    assert got.lease_expires_at > timezone.now()
    assert got.attempts == 1


def test_claim_writes_attempt_row():
    t = make_task()
    claim("w1", lease_seconds=900)
    assert Attempt.objects.filter(task=t).count() == 1


def test_claim_returns_none_when_board_empty():
    assert claim("w1", lease_seconds=900) is None


def test_claim_skips_manual_cards():
    make_task(autonomy="MANUAL", title="manual")
    assert claim("w1", lease_seconds=900) is None


def test_claim_skips_cards_with_unmet_deps():
    blocker = make_task(title="blocker", status=Status.IN_PROGRESS)
    blocked = make_task(title="blocked")
    from board.models import TaskDep

    TaskDep.objects.create(task=blocked, depends_on=blocker)
    assert claim("w1", lease_seconds=900) is None


def test_claim_respects_kind_filter():
    make_task(title="ops", kind="ops")
    make_task(title="code", kind="code")
    got = claim("w1", lease_seconds=900, allowed_kinds=["code"])
    assert got.kind == "code"


def test_two_workers_do_not_claim_same_card():
    make_task(title="only one")
    a = claim("w1", lease_seconds=900)
    b = claim("w2", lease_seconds=900)
    assert a is not None
    assert b is None


# --------------------------------------------------------------------------
# heartbeat
# --------------------------------------------------------------------------


def test_heartbeat_updates_liveness_and_step():
    t = make_task()
    got = claim("w1", lease_seconds=900)
    old = got.heartbeat_at
    got.heartbeat_at = old - timezone.timedelta(minutes=5)
    got.save()

    heartbeat(got, "шаг 3/7: генерирую embeddings")
    got.refresh_from_db()
    assert got.heartbeat_at > old
    assert "embeddings" in got.current_step


def test_heartbeat_truncates_long_step():
    t = make_task()
    got = claim("w1", lease_seconds=900)
    heartbeat(got, "x" * 1000)
    got.refresh_from_db()
    assert len(got.current_step) <= 300


# --------------------------------------------------------------------------
# sweep / crash recovery
# --------------------------------------------------------------------------


def test_sweep_returns_expired_lease_to_ready():
    t = make_task()
    got = claim("w1", lease_seconds=900)
    Task.objects.filter(pk=got.pk).update(
        lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
    )

    n = sweep_leases(lease_seconds=900)
    assert n == 1
    got.refresh_from_db()
    assert got.status == Status.READY
    assert got.claimed_by == ""
    assert got.lease_expires_at is None


def test_sweep_leaves_valid_lease_alone():
    got = claim("w1", lease_seconds=900)
    n = sweep_leases(lease_seconds=900)
    assert n == 0
    got.refresh_from_db()
    assert got.status == Status.IN_PROGRESS


def test_sweep_bumps_stuck_score():
    got = claim("w1", lease_seconds=900)
    Task.objects.filter(pk=got.pk).update(
        lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
    )
    sweep_leases(lease_seconds=900)
    got.refresh_from_db()
    assert got.stuck_score == 1.0

    Task.objects.filter(pk=got.pk).update(
        lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
    )
    sweep_leases(lease_seconds=900)
    got.refresh_from_db()
    assert got.stuck_score == 2.0


def test_sweep_logs_lease_expired_event():
    got = claim("w1", lease_seconds=900)
    Task.objects.filter(pk=got.pk).update(
        lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
    )
    sweep_leases(lease_seconds=900)
    assert got.events.filter(event="lease_expired").count() == 1


def test_stuck_score_is_capped():
    got = claim("w1", lease_seconds=900)
    Task.objects.filter(pk=got.pk).update(
        lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
    )
    for _ in range(15):
        Task.objects.filter(pk=got.pk).update(
            lease_expires_at=timezone.now() - timezone.timedelta(seconds=1)
        )
        sweep_leases(lease_seconds=900)
    got.refresh_from_db()
    assert got.stuck_score == 10.0


# --------------------------------------------------------------------------
# unblock
# --------------------------------------------------------------------------


def test_unblock_moves_card_when_deps_done():
    from board.models import TaskDep

    blocker = make_task(title="blocker", status=Status.IN_PROGRESS)
    blocked = make_task(title="blocked", status=Status.BLOCKED)
    TaskDep.objects.create(task=blocked, depends_on=blocker)

    assert unblock_ready() == 0
    blocker.status = Status.DONE
    blocker.save()

    assert unblock_ready() == 1
    blocked.refresh_from_db()
    assert blocked.status == Status.READY


# --------------------------------------------------------------------------
# budget enforcement during real lifecycle
# --------------------------------------------------------------------------


def test_card_fails_after_budget_exhausted_via_claim():
    t = make_task(max_attempts=2)
    for _ in range(2):
        got = claim("w1", lease_seconds=900)
        assert got is not None
        transition(
            got, Status.REVIEW, Ctx(actor=Actor.AGENT, evidence={"ok": 1})
        )
        transition(got, Status.IN_PROGRESS, Ctx(actor=Actor.AGENT))

    assert claim("w1", lease_seconds=900) is None
