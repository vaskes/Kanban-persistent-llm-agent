"""
State machine for the task board.

Every state change goes through `transition()`. There is no other way to move a
card. That matters: the guards below (acceptance required, evidence required,
budget caps, autonomy enforcement) are the *only* place those rules exist, so
there is exactly one place to audit and exactly one place to break.

Guards are deliberately structural, not advisory:
  * INBOX -> BACKLOG requires acceptance text
  * IN_PROGRESS -> REVIEW requires evidence
  * REVIEW -> DONE is refused for autonomy=MANUAL unless the actor is an operator
  * any attempt that would exceed max_attempts is refused
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from django.db import transaction
from django.utils import timezone

from .models import Actor, Attempt, Status, Task, TaskEvent


class TransitionError(Exception):
    """Raised when a transition is not permitted. Carries a machine-readable reason."""


@dataclass(frozen=True)
class Guard:
    name: str
    fn: Callable[[Task, "Ctx"], None]


@dataclass
class Ctx:
    """Everything a transition needs besides the card itself."""

    actor: str = Actor.SYSTEM
    reason: str = ""
    evidence: dict | None = None
    allow_budget_override: bool = False
    extra: dict | None = None

    def __post_init__(self) -> None:
        if self.extra is None:
            self.extra = {}


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------


def _g_acceptance(task: Task, ctx: Ctx) -> None:
    if not task.acceptance.strip():
        raise TransitionError("acceptance criteria required to leave INBOX")


def _g_deps_closed(task: Task, ctx: Ctx) -> None:
    unmet = task.unmet_dependencies()
    if unmet:
        raise TransitionError(f"unmet dependencies: {','.join(unmet)}")


def _g_budget(task: Task, ctx: Ctx) -> None:
    if ctx.allow_budget_override or ctx.actor == Actor.OPERATOR:
        return
    if task.attempts_exhausted:
        raise TransitionError(
            f"attempts exhausted ({task.attempts}/{task.max_attempts})"
        )
    if task.tokens_exhausted:
        raise TransitionError(
            f"token budget exhausted ({task.tokens_used}/{task.max_tokens})"
        )


def _g_evidence(task: Task, ctx: Ctx) -> None:
    ev = ctx.evidence if ctx.evidence is not None else task.evidence
    if not ev:
        raise TransitionError("evidence required to move IN_PROGRESS -> REVIEW")


def _g_manual_autonomy(task: Task, ctx: Ctx) -> None:
    if task.autonomy == "MANUAL" and ctx.actor != Actor.OPERATOR:
        raise TransitionError("autonomy=MANUAL requires operator action")


def _g_operator_only(task: Task, ctx: Ctx) -> None:
    if ctx.actor != Actor.OPERATOR:
        raise TransitionError(f"{ctx.actor} may not perform this transition")


def _g_no_children_running(task: Task, ctx: Ctx) -> None:
    running = task.children.exclude(
        status__in=[Status.DONE, Status.CANCELLED, Status.FAILED]
    ).count()
    if running:
        raise TransitionError(f"{running} subtask(s) not closed")


# --------------------------------------------------------------------------
# transition table:  (from, to) -> guards
# --------------------------------------------------------------------------

TRANSITIONS: dict[tuple[str, str], list[Guard]] = {
    # intake
    (Status.INBOX, Status.BACKLOG): [
        Guard("acceptance", _g_acceptance),
    ],
    (Status.INBOX, Status.CANCELLED): [],
    (Status.INBOX, Status.NEEDS_HUMAN): [],

    # backlog
    (Status.BACKLOG, Status.READY): [Guard("deps", _g_deps_closed)],
    (Status.BACKLOG, Status.BLOCKED): [],
    (Status.BACKLOG, Status.NEEDS_HUMAN): [],
    (Status.BACKLOG, Status.CANCELLED): [Guard("operator", _g_operator_only)],

    # blocked -> ready is driven by the sweeper once deps close
    (Status.BLOCKED, Status.READY): [Guard("deps", _g_deps_closed)],

    # execution
    (Status.READY, Status.IN_PROGRESS): [
        Guard("deps", _g_deps_closed),
        Guard("manual", _g_manual_autonomy),
        Guard("budget", _g_budget),
    ],
    (Status.IN_PROGRESS, Status.REVIEW): [
        Guard("evidence", _g_evidence),
    ],
    (Status.IN_PROGRESS, Status.READY): [],  # release / lease expiry
    (Status.IN_PROGRESS, Status.BLOCKED): [],
    (Status.IN_PROGRESS, Status.NEEDS_HUMAN): [],
    (Status.IN_PROGRESS, Status.FAILED): [Guard("operator", _g_operator_only)],

    # verification
    (Status.REVIEW, Status.DONE): [
        Guard("manual", _g_manual_autonomy),
        Guard("children", _g_no_children_running),
    ],
    (Status.REVIEW, Status.IN_PROGRESS): [Guard("budget", _g_budget)],
    (Status.REVIEW, Status.FAILED): [Guard("operator", _g_operator_only)],
    (Status.REVIEW, Status.NEEDS_HUMAN): [],

    # terminal-ish
    (Status.NEEDS_HUMAN, Status.BACKLOG): [Guard("operator", _g_operator_only)],
    (Status.NEEDS_HUMAN, Status.READY): [Guard("operator", _g_operator_only)],
    (Status.NEEDS_HUMAN, Status.FAILED): [Guard("operator", _g_operator_only)],
    (Status.NEEDS_HUMAN, Status.CANCELLED): [Guard("operator", _g_operator_only)],
    (Status.FAILED, Status.BACKLOG): [Guard("operator", _g_operator_only)],
    (Status.DONE, Status.ARCHIVED): [],
    (Status.DONE, Status.READY): [Guard("operator", _g_operator_only)],
    (Status.ARCHIVED, Status.BACKLOG): [Guard("operator", _g_operator_only)],
}

TERMINAL = {Status.CANCELLED}


def allowed_transitions(status: str) -> list[str]:
    return [to for (frm, to) in TRANSITIONS if frm == status]


@transaction.atomic
def transition(
    task: Task,
    to: str,
    ctx: Ctx | None = None,
    event: str = "transition",
) -> Task:
    """Move `task` to `to`, enforcing every guard. Raises TransitionError."""
    ctx = ctx or Ctx()
    frm = task.status

    if to == frm:
        return task
    if frm in TERMINAL:
        raise TransitionError(f"{frm} is terminal")

    guards = TRANSITIONS.get((frm, to))
    if guards is None:
        allowed = allowed_transitions(frm)
        raise TransitionError(
            f"no transition {frm} -> {to} (allowed: {','.join(allowed) or 'none'})"
        )

    for g in guards:
        g.fn(task, ctx)

    payload = dict(ctx.extra or {})
    if ctx.reason:
        payload["reason"] = ctx.reason

    if ctx.evidence is not None:
        task.evidence = ctx.evidence

    task.status = to
    if to in {Status.DONE, Status.FAILED, Status.CANCELLED, Status.ARCHIVED}:
        task.closed_at = timezone.now()
    if to in {
        Status.READY,
        Status.BACKLOG,
        Status.INBOX,
        Status.BLOCKED,
        Status.NEEDS_HUMAN,
        Status.REVIEW,
        Status.DONE,
        Status.FAILED,
        Status.CANCELLED,
        Status.ARCHIVED,
    }:
        # Leaving IN_PROGRESS means nobody holds the card any more. Clearing
        # the lease here is what keeps a released card from still displaying
        # "claimed by ..." with a stale expiry — which the operator would read
        # as "busy" on a card that is actually free.
        task.claimed_by = ""
        task.lease_expires_at = None
        task.current_step = ""
    if to in {Status.READY, Status.BACKLOG, Status.INBOX, Status.REVIEW}:
        task.needs_human = False
        task.attention_reason = ""
    if to == Status.NEEDS_HUMAN:
        task.needs_human = True
        task.attention_reason = ctx.reason or "operator attention required"
    if to in {Status.DONE, Status.IN_PROGRESS}:
        task.progress_pct = 100 if to == Status.DONE else task.progress_pct
    if to == Status.REVIEW:
        # real progress: the executor produced evidence, so the card is not
        # stuck any more. Decay rather than reset, so a card that churns
        # between DONE and reopened still carries some signal.
        task.stuck_score = max(task.stuck_score - 1.0, 0.0)

    task.save()

    TaskEvent.objects.create(
        task=task,
        actor=ctx.actor,
        event=event,
        from_status=frm,
        to_status=to,
        payload=payload,
    )
    return task


# --------------------------------------------------------------------------
# lifecycle helpers
# --------------------------------------------------------------------------


@transaction.atomic
def claim(worker: str, lease_seconds: int, allowed_kinds: list[str] | None = None) -> Task | None:
    """
    Atomically take one READY card.

    Uses SELECT ... FOR UPDATE SKIP LOCKED so that multiple workers can claim
    concurrently without ever blocking each other and without any explicit
    locking protocol.
    """
    qs = Task.objects.select_for_update(skip_locked=True).filter(status=Status.READY)
    if allowed_kinds:
        qs = qs.filter(kind__in=allowed_kinds)
    task = qs.order_by("-priority", "created_at").first()
    if task is None:
        return None
    if task.unmet_dependencies():
        return None
    if task.autonomy == "MANUAL":
        return None

    # Budget caps are enforced here, not only in transition(). claim() writes
    # status directly (it must, to hold the row lock), so without this check
    # the whole budget guarantee could be bypassed by taking a card.
    if task.attempts_exhausted or task.tokens_exhausted:
        TaskEvent.objects.create(
            task=task,
            actor=Actor.SYSTEM,
            event="claim_refused",
            from_status=Status.READY,
            to_status=Status.READY,
            payload={
                "reason": "attempts_exhausted"
                if task.attempts_exhausted
                else "tokens_exhausted",
                "attempts": task.attempts,
                "max_attempts": task.max_attempts,
                "tokens_used": task.tokens_used,
                "max_tokens": task.max_tokens,
            },
        )
        return None

    task.status = Status.IN_PROGRESS
    task.claimed_by = worker
    task.lease_expires_at = timezone.now() + timezone.timedelta(seconds=lease_seconds)
    task.heartbeat_at = timezone.now()
    task.attempts += 1
    # NOTE: stuck_score is deliberately NOT reset here. It is cumulative
    # evidence that this card keeps dying, and it must survive re-claims or the
    # operator can never see a card that has failed five times in a row. It
    # decays on real progress instead (see transition() to REVIEW).
    task.save()

    Attempt.objects.create(task=task, started_at=timezone.now())
    TaskEvent.objects.create(
        task=task,
        actor=Actor.AGENT,
        event="claim",
        from_status=Status.READY,
        to_status=Status.IN_PROGRESS,
        payload={"worker": worker, "attempt": task.attempts},
    )
    return task


def heartbeat(task: Task, step: str = "") -> None:
    """Cheap liveness signal. Called every TASK_HEARTBEAT_SECONDS during work."""
    updates: dict = {"heartbeat_at": timezone.now()}
    if step:
        updates["current_step"] = step[:300]
    Task.objects.filter(pk=task.pk).update(**updates)
    task.heartbeat_at = updates["heartbeat_at"]
    if step:
        task.current_step = step[:300]


def sweep_leases(lease_seconds: int) -> int:
    """
    Return IN_PROGRESS cards with expired leases to READY and bump stuck_score.

    This is the crash-recovery path: if a worker dies mid-task, the card comes
    back by itself. No operator action required.
    """
    now = timezone.now()
    expired = Task.objects.filter(
        status=Status.IN_PROGRESS, lease_expires_at__lt=now
    )
    count = 0
    for task in expired.iterator():
        before = task.status
        task.status = Status.READY
        task.claimed_by = ""
        task.lease_expires_at = None
        task.stuck_score = min(task.stuck_score + 1.0, 10.0)
        task.save()
        TaskEvent.objects.create(
            task=task,
            actor=Actor.SYSTEM,
            event="lease_expired",
            from_status=before,
            to_status=Status.READY,
            payload={"attempts": task.attempts},
        )
        count += 1
    return count


def unblock_ready() -> int:
    """Move BLOCKED -> READY once all dependencies are DONE."""
    count = 0
    for task in Task.objects.filter(status=Status.BLOCKED).iterator():
        if not task.unmet_dependencies():
            try:
                transition(
                    task,
                    Status.READY,
                    Ctx(actor=Actor.SYSTEM, reason="dependencies satisfied"),
                    event="unblocked",
                )
                count += 1
            except TransitionError:
                continue
    return count
