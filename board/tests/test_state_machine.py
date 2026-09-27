"""
Unit tests for the state machine guards.

These are the tests that matter most: every one of them encodes a rule that the
operator relies on to keep an autonomous agent from lying to them.
"""

import pytest
from django.utils import timezone

from board.models import Actor, Status, Task, TaskDep
from board.state import Ctx, TransitionError, transition

pytestmark = pytest.mark.django_db


def make_task(**kw) -> Task:
    defaults = dict(
        title="test card",
        acceptance="file exists and is non-empty",
        status=Status.BACKLOG,
    )
    defaults.update(kw)
    return Task.objects.create(**defaults)


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_full_happy_path_inbox_to_done():
    t = make_task(status=Status.INBOX, acceptance="report written")
    assert t.status == Status.INBOX

    transition(t, Status.BACKLOG, Ctx(actor=Actor.OPERATOR))
    assert t.status == Status.BACKLOG

    transition(t, Status.READY, Ctx(actor=Actor.SYSTEM))
    transition(t, Status.IN_PROGRESS, Ctx(actor=Actor.AGENT))
    assert t.status == Status.IN_PROGRESS

    transition(
        t, Status.REVIEW, Ctx(actor=Actor.AGENT, evidence={"exit_code": 0, "file": "out.md"})
    )
    assert t.status == Status.REVIEW

    transition(t, Status.DONE, Ctx(actor=Actor.VERIFIER))
    assert t.status == Status.DONE
    assert t.closed_at is not None


# --------------------------------------------------------------------------
# guard: acceptance
# --------------------------------------------------------------------------


def test_cannot_leave_inbox_without_acceptance():
    t = make_task(status=Status.INBOX, acceptance="")
    with pytest.raises(TransitionError, match="acceptance"):
        transition(t, Status.BACKLOG, Ctx(actor=Actor.OPERATOR))
    assert Task.objects.get(pk=t.pk).status == Status.INBOX


def test_whitespace_acceptance_does_not_count():
    t = make_task(status=Status.INBOX, acceptance="   \n ")
    with pytest.raises(TransitionError, match="acceptance"):
        transition(t, Status.BACKLOG, Ctx(actor=Actor.OPERATOR))


# --------------------------------------------------------------------------
# guard: evidence
# --------------------------------------------------------------------------


def test_cannot_enter_review_without_evidence():
    t = make_task(status=Status.IN_PROGRESS)
    with pytest.raises(TransitionError, match="evidence"):
        transition(t, Status.REVIEW, Ctx(actor=Actor.AGENT))
    assert Task.objects.get(pk=t.pk).status == Status.IN_PROGRESS


def test_evidence_may_be_supplied_at_transition_time():
    t = make_task(status=Status.IN_PROGRESS)
    transition(t, Status.REVIEW, Ctx(actor=Actor.AGENT, evidence={"exit_code": 0}))
    t.refresh_from_db()
    assert t.evidence == {"exit_code": 0}


# --------------------------------------------------------------------------
# guard: autonomy
# --------------------------------------------------------------------------


def test_manual_card_cannot_be_claimed_by_agent():
    t = make_task(status=Status.READY, autonomy="MANUAL")
    with pytest.raises(TransitionError, match="MANUAL"):
        transition(t, Status.IN_PROGRESS, Ctx(actor=Actor.AGENT))


def test_manual_card_can_be_moved_by_operator():
    t = make_task(status=Status.READY, autonomy="MANUAL")
    transition(t, Status.IN_PROGRESS, Ctx(actor=Actor.OPERATOR))
    assert t.status == Status.IN_PROGRESS


def test_manual_card_cannot_self_approve_done():
    t = make_task(status=Status.REVIEW, autonomy="MANUAL", evidence={"ok": 1})
    with pytest.raises(TransitionError, match="MANUAL"):
        transition(t, Status.DONE, Ctx(actor=Actor.VERIFIER))
    transition(t, Status.DONE, Ctx(actor=Actor.OPERATOR))
    assert t.status == Status.DONE


def test_ask_autonomy_allows_agent_to_execute():
    t = make_task(status=Status.READY, autonomy="ASK")
    transition(t, Status.IN_PROGRESS, Ctx(actor=Actor.AGENT))
    assert t.status == Status.IN_PROGRESS


# --------------------------------------------------------------------------
# guard: budget
# --------------------------------------------------------------------------


def test_cannot_claim_when_attempts_exhausted():
    t = make_task(status=Status.READY, max_attempts=2, attempts=2)
    with pytest.raises(TransitionError, match="attempts exhausted"):
        transition(t, Status.IN_PROGRESS, Ctx(actor=Actor.AGENT))


def test_cannot_claim_when_token_budget_exhausted():
    t = make_task(status=Status.READY, max_tokens=1000, tokens_used=1000)
    with pytest.raises(TransitionError, match="token budget exhausted"):
        transition(t, Status.IN_PROGRESS, Ctx(actor=Actor.AGENT))


def test_operator_may_override_budget():
    t = make_task(status=Status.READY, max_attempts=1, attempts=1)
    transition(t, Status.IN_PROGRESS, Ctx(actor=Actor.OPERATOR))
    assert t.status == Status.IN_PROGRESS


# --------------------------------------------------------------------------
# guard: dependencies
# --------------------------------------------------------------------------


def test_cannot_be_ready_with_unmet_dependency():
    a = make_task(title="A", status=Status.IN_PROGRESS)
    b = make_task(title="B", status=Status.BACKLOG)
    TaskDep.objects.create(task=b, depends_on=a)

    with pytest.raises(TransitionError, match="unmet dependencies"):
        transition(b, Status.READY, Ctx(actor=Actor.SYSTEM))


def test_ready_when_dependency_done():
    a = make_task(title="A", status=Status.DONE)
    b = make_task(title="B", status=Status.BACKLOG)
    TaskDep.objects.create(task=b, depends_on=a)

    assert b.unmet_dependencies() == []
    transition(b, Status.READY, Ctx(actor=Actor.SYSTEM))
    assert b.status == Status.READY


def test_dependency_cannot_be_self():
    a = make_task()
    with pytest.raises(Exception):
        TaskDep.objects.create(task=a, depends_on=a)


# --------------------------------------------------------------------------
# guard: children
# --------------------------------------------------------------------------


def test_parent_cannot_close_while_subtasks_open():
    parent = make_task(title="parent", status=Status.REVIEW, evidence={"ok": 1})
    child = make_task(title="child", status=Status.IN_PROGRESS, parent=parent)
    with pytest.raises(TransitionError, match="subtask"):
        transition(parent, Status.DONE, Ctx(actor=Actor.OPERATOR))

    child.status = Status.DONE
    child.save()
    parent.refresh_from_db()
    transition(parent, Status.DONE, Ctx(actor=Actor.OPERATOR))
    assert parent.status == Status.DONE


# --------------------------------------------------------------------------
# operator-only transitions
# --------------------------------------------------------------------------


def test_agent_cannot_cancel_from_backlog():
    t = make_task(status=Status.BACKLOG)
    with pytest.raises(TransitionError, match="may not"):
        transition(t, Status.CANCELLED, Ctx(actor=Actor.AGENT))


def test_cancelled_is_terminal():
    t = make_task(status=Status.CANCELLED)
    with pytest.raises(TransitionError, match="terminal"):
        transition(t, Status.BACKLOG, Ctx(actor=Actor.OPERATOR))


# --------------------------------------------------------------------------
# invalid transitions
# --------------------------------------------------------------------------


def test_no_skip_from_inbox_to_done():
    t = make_task(status=Status.INBOX, acceptance="x")
    with pytest.raises(TransitionError, match="no transition"):
        transition(t, Status.DONE, Ctx(actor=Actor.OPERATOR))


def test_transition_to_same_status_is_noop():
    t = make_task(status=Status.BACKLOG)
    transition(t, Status.BACKLOG, Ctx(actor=Actor.OPERATOR))
    assert t.status == Status.BACKLOG


# --------------------------------------------------------------------------
# audit log
# --------------------------------------------------------------------------


def test_every_transition_writes_an_event():
    t = make_task(status=Status.INBOX, acceptance="x")
    transition(t, Status.BACKLOG, Ctx(actor=Actor.OPERATOR, reason="triaged"))
    ev = t.events.order_by("ts").last()
    assert ev.from_status == Status.INBOX
    assert ev.to_status == Status.BACKLOG
    assert ev.actor == Actor.OPERATOR
    assert ev.payload["reason"] == "triaged"


def test_needs_human_sets_attention_flag():
    t = make_task(status=Status.IN_PROGRESS)
    transition(t, Status.NEEDS_HUMAN, Ctx(actor=Actor.AGENT, reason="cannot infer style"))
    t.refresh_from_db()
    assert t.needs_human is True
    assert "style" in t.attention_reason
