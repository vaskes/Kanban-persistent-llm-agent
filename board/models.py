"""
Data model for the kanban task board.

Design rules:
  * The board IS the state store. There is no hidden in-process agent state.
  * `task_events` is append-only. It is written by a database trigger so that
    state changes cannot happen without an audit row, even if application code
    forgets to log.
  * Budgets (`max_attempts`, `max_tokens`) are per-card hard caps, not
    conventions. The state machine refuses transitions that would exceed them.
  * Autonomy (`AUTO` / `ASK` / `MANUAL`) is enforced on transitions, so an
    agent cannot talk itself into an `AUTO` decision.
"""

from __future__ import annotations

import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


class Status(models.TextChoices):
    INBOX = "INBOX", "Inbox"
    BACKLOG = "BACKLOG", "Backlog"
    READY = "READY", "Ready"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    REVIEW = "REVIEW", "Review"
    DONE = "DONE", "Done"
    BLOCKED = "BLOCKED", "Blocked"
    NEEDS_HUMAN = "NEEDS_HUMAN", "Needs human"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"
    ARCHIVED = "ARCHIVED", "Archived"


class Autonomy(models.TextChoices):
    AUTO = "AUTO", "Auto"
    ASK = "ASK", "Ask"
    MANUAL = "MANUAL", "Manual"


class Kind(models.TextChoices):
    RESEARCH = "research", "Research"
    CODE = "code", "Code"
    OPS = "ops", "Ops"
    ANALYSIS = "analysis", "Analysis"
    CHORE = "chore", "Chore"


class VerifierKind(models.TextChoices):
    COMMAND = "command", "Command exit code"
    FILE_EXISTS = "file_exists", "File exists"
    LLM_JUDGE = "llm_judge", "LLM judge"
    NONE = "none", "None"


class Actor(models.TextChoices):
    AGENT = "agent", "Agent"
    OPERATOR = "operator", "Operator"
    SYSTEM = "system", "System"
    VERIFIER = "verifier", "Verifier"


def _uuid() -> str:
    return uuid.uuid4().hex


class TaskQuerySet(models.QuerySet):
    def ready(self):
        return self.filter(status=Status.READY)

    def active(self):
        return self.filter(
            status__in=[Status.IN_PROGRESS, Status.REVIEW, Status.BLOCKED]
        )


class Task(models.Model):
    # --- identity ---
    id = models.CharField(primary_key=True, max_length=32, default=_uuid, editable=False)
    title = models.CharField(max_length=500)
    goal = models.TextField(blank=True, default="")
    kind = models.CharField(
        max_length=32, choices=Kind.choices, default=Kind.CHORE, db_index=True
    )
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.INBOX, db_index=True
    )

    # --- control ---
    priority = models.IntegerField(default=5, db_index=True)
    priority_reason = models.TextField(blank=True, default="")
    autonomy = models.CharField(
        max_length=16, choices=Autonomy.choices, default=Autonomy.ASK
    )
    created_by = models.CharField(max_length=32, default=Actor.OPERATOR)

    # --- decomposition tree ---
    root_id = models.CharField(max_length=32, blank=True, default="", db_index=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    depth = models.IntegerField(default=0)

    # --- specification (a card cannot leave INBOX without acceptance) ---
    acceptance = models.TextField(blank=True, default="")
    verifier_kind = models.CharField(
        max_length=32, choices=VerifierKind.choices, default=VerifierKind.NONE
    )
    verifier_cmd = models.TextField(blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)

    # --- budgets (hard caps) ---
    max_attempts = models.IntegerField(default=settings.TASK_DEFAULT_MAX_ATTEMPTS)
    max_tokens = models.IntegerField(default=settings.TASK_DEFAULT_MAX_TOKENS)
    deadline_at = models.DateTimeField(null=True, blank=True)

    # --- execution ---
    claimed_by = models.CharField(max_length=64, blank=True, default="", db_index=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    current_step = models.CharField(max_length=300, blank=True, default="")
    attempts = models.IntegerField(default=0)
    tokens_used = models.IntegerField(default=0)
    progress_pct = models.IntegerField(default=0)

    # --- operator signals ---
    needs_human = models.BooleanField(default=False)
    attention_reason = models.TextField(blank=True, default="")
    stuck_score = models.FloatField(default=0.0, db_index=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = TaskQuerySet.as_manager()

    class Meta:
        db_table = "tasks"
        indexes = [
            models.Index(fields=["status", "-priority", "created_at"]),
            models.Index(fields=["status", "lease_expires_at"]),
        ]

    def __str__(self) -> str:
        return f"[{self.status}] {self.title[:60]}"

    # --- derived budget checks used by the state machine ---
    @property
    def attempts_exhausted(self) -> bool:
        return self.attempts >= self.max_attempts

    @property
    def tokens_exhausted(self) -> bool:
        return self.tokens_used >= self.max_tokens

    @property
    def budget_exhausted(self) -> bool:
        return self.attempts_exhausted or self.tokens_exhausted

    @property
    def lease_expired(self) -> bool:
        return self.lease_expires_at is not None and self.lease_expires_at < timezone.now()

    def unmet_dependencies(self) -> list[str]:
        return [
            d.depends_on_id
            for d in self.dependencies.all()
            if d.depends_on.status != Status.DONE
        ]

    def has_evidence(self) -> bool:
        return bool(self.evidence)


class TaskDep(models.Model):
    """DAG edge. `task` cannot be claimed until every `depends_on` is DONE."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="dependencies")
    depends_on = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name="dependents"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "task_deps"
        constraints = [
            models.UniqueConstraint(fields=["task", "depends_on"], name="uniq_dep"),
            models.CheckConstraint(
                condition=~models.Q(task=models.F("depends_on")),
                name="dep_not_self",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.task_id} <- {self.depends_on_id}"


class TaskEvent(models.Model):
    """Append-only audit log. Written by trigger as well as by application code."""

    id = models.BigAutoField(primary_key=True)
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name="events", null=True, blank=True
    )
    ts = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=32, default=Actor.SYSTEM)
    event = models.CharField(max_length=64, db_index=True)
    from_status = models.CharField(max_length=32, blank=True, default="")
    to_status = models.CharField(max_length=32, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "task_events"
        indexes = [models.Index(fields=["task", "ts"])]

    def __str__(self) -> str:
        return f"{self.ts:%H:%M:%S} {self.actor}/{self.event} {self.task_id or '-'}"


class Attempt(models.Model):
    """One execution attempt. In part 1 this is written by tests and operators;
    the runtime in part 2 writes it for real."""

    id = models.CharField(primary_key=True, max_length=32, default=_uuid, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attempts_log")
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=32, blank=True, default="")
    tokens_in = models.IntegerField(default=0)
    tokens_out = models.IntegerField(default=0)
    transcript = models.TextField(blank=True, default="")
    score = models.FloatField(null=True, blank=True)
    reflection = models.TextField(blank=True, default="")
    error_class = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "attempts"
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.task_id} attempt {self.pk[:8]} {self.outcome or 'running'}"


class Memory(models.Model):
    """
    Placeholder for the learning layer (part 2+).

    Deliberately present in part 1 so that the schema does not have to be
    re-migrated once the retrieval/digestor work starts. Nothing writes here yet.
    """

    MEMORY_KIND = [
        ("fact", "Fact"),
        ("lesson", "Lesson"),
        ("procedure", "Procedure"),
        ("preference", "Preference"),
        ("gotcha", "Gotcha"),
    ]

    id = models.CharField(primary_key=True, max_length=32, default=_uuid, editable=False)
    kind = models.CharField(max_length=32, choices=MEMORY_KIND, default="lesson")
    content = models.TextField()
    source_task = models.ForeignKey(
        Task, on_delete=models.SET_NULL, null=True, blank=True, related_name="memories"
    )
    confidence = models.FloatField(default=0.5)
    use_count = models.IntegerField(default=0)
    success_rate = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "memories"

    def __str__(self) -> str:
        return f"{self.kind}: {self.content[:50]}"
