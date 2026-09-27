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

import hashlib
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

    # --- placement in the project tree ---
    project = models.ForeignKey(
        "Project", on_delete=models.CASCADE, null=True, blank=True,
        related_name="tasks",
        help_text="null only for tasks created before projects existed",
    )
    backlog = models.ForeignKey(
        "Backlog", on_delete=models.CASCADE, null=True, blank=True,
        related_name="tasks",
    )
    assignee = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_tasks",
        help_text="null means any agent may take it",
    )

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


class AgentApiKey(models.Model):
    """
    Long-lived credential for agents and scripts.

    Why this exists: an agent runs headless, with no browser and no session. It
    needs a credential it can put in an Authorization header and forget about.
    Session cookies are the wrong tool — they are ambient credentials, which is
    exactly why they need CSRF protection and why a worker process holding one
    is a liability.

    Only a SHA-256 hash is stored. The plaintext is shown once at creation and
    never again, so a database leak does not yield usable keys.
    """

    class Scope(models.TextChoices):
        READ = "read", "Read"
        WRITE = "write", "Read + write (claim, heartbeat, review)"

    id = models.CharField(primary_key=True, max_length=32, default=_uuid, editable=False)
    prefix = models.CharField(
        max_length=12, unique=True, db_index=True,
        help_text="public identifier, safe to log; the secret is never stored",
    )
    secret_hash = models.CharField(max_length=64, db_index=True)
    label = models.CharField(max_length=120, blank=True, default="")
    scope = models.CharField(max_length=16, choices=Scope.choices, default=Scope.WRITE)
    # The account this key acts as. Nullable so a key can belong purely to
    # an Agent (auto-minted on agent registration) — in that case permissions
    # are derived from the agent's other relationships. When both are set,
    # user wins, because the existing permission helpers are user-keyed.
    user = models.ForeignKey(
        "auth.User", on_delete=models.CASCADE, null=True, blank=True,
        related_name="api_keys",
        help_text="the account this key acts as; permissions are inherited",
    )
    agent = models.ForeignKey(
        "Agent", on_delete=models.CASCADE, null=True, blank=True,
        related_name="api_keys",
        help_text="the agent this key was minted for, if any",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    use_count = models.BigIntegerField(default=0)

    class Meta:
        db_table = "agent_api_keys"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.prefix}… ({self.label or 'unlabelled'}, {self.scope})"

    @property
    def can_write(self) -> bool:
        return self.scope == self.Scope.WRITE

    def check_secret(self, secret: str) -> bool:
        return self.hash_secret(secret) == self.secret_hash

    @staticmethod
    def hash_secret(secret: str) -> str:
        """SHA-256 of the secret. Centralised so minting and matching agree."""
        return hashlib.sha256(secret.encode()).hexdigest()

    def mark_used(self) -> None:
        AgentApiKey.objects.filter(pk=self.pk).update(
            last_used_at=timezone.now(), use_count=self.use_count + 1
        )


# ---------------------------------------------------------------------------
# tree:  Project  ->  Backlog  ->  Task
# ---------------------------------------------------------------------------

DEFAULT_PROJECT_KEY = "kanban-agent"
DEFAULT_PROJECT_NAME = "kanban-agent"
DEFAULT_PROJECT_REPO = "https://github.com/vaskes/Kanban-persistent-llm-agent"


class Project(models.Model):
    """
    Top of the tree. Always contains the default project, which is this
    repository — the work we do on the system itself lives there.
    """

    key = models.SlugField(
        max_length=80, unique=True,
        help_text="stable identifier used in URLs; never changes once created",
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    repo_url = models.URLField(blank=True, default="")
    is_default = models.BooleanField(
        default=False,
        help_text="the project this repository itself; cannot be deleted",
    )
    # Nullable on purpose: the default project is created by a data migration
    # before any account exists, so at that moment there is genuinely no owner.
    # on_delete=PROTECT still prevents deleting a user who created real work.
    created_by = models.ForeignKey(
        "auth.User", on_delete=models.PROTECT, null=True, blank=True,
        related_name="created_projects",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    archived = models.BooleanField(default=False)

    class Meta:
        db_table = "projects"
        ordering = ["-is_default", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"

    def save(self, *args, **kwargs):
        # The default project is the anchor of the whole tree. If it were ever
        # renamed or re-keyed, every link and every stored chat scope pointing
        # at it would break at once.
        if self.is_default:
            self.key = DEFAULT_PROJECT_KEY
            self.name = DEFAULT_PROJECT_NAME
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_default:
            raise ProtectedDefaultProject(
                "the default project cannot be deleted; archive it instead"
            )
        return super().delete(*args, **kwargs)


class ProtectedDefaultProject(Exception):
    """Raised on any attempt to remove the default project."""


class ProjectMembership(models.Model):
    """
    Binary read/write access to one project.

    Deliberately two levels, not a role system: the operator grants access, and
    finer policy has not been needed. Absence of a row means no access.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        "auth.User", on_delete=models.CASCADE, related_name="project_memberships"
    )
    can_write = models.BooleanField(default=False)
    granted_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, related_name="+"
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "project_memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["project", "user"], name="uniq_project_member"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.project_id} {'rw' if self.can_write else 'ro'}"


class Agent(models.Model):
    """
    A registered worker or assistant.

    Several can exist. Each task may name one as its assignee, or leave it
    unset to mean "any agent may take this".
    """

    class Status(models.TextChoices):
        REACHABLE = "reachable", "Reachable"
        UNREACHABLE = "unreachable", "Unreachable"

    id = models.CharField(primary_key=True, max_length=32, default=_uuid, editable=False)
    name = models.CharField(max_length=80, unique=True)
    kind = models.CharField(
        max_length=32,
        choices=[("worker", "Worker"), ("assistant", "Assistant")],
        default="worker",
    )
    base_url = models.CharField(
        max_length=300, blank=True, default="",
        help_text="where to reach it; empty means local-in-process",
    )
    api_key_id = models.ForeignKey(
        "AgentApiKey", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agents",
        help_text="the credential this agent authenticates with",
    )
    registered_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    note = models.TextField(blank=True, default="")

    # --- outbound model the agent uses (provider + endpoint + key) ---
    # None / empty means "this agent has no LLM backend registered"; the runtime
    # then refuses to dispatch a task to it. A row with a provider but no
    # base_url is also invalid and is caught by clean().
    model_provider = models.CharField(
        max_length=32, blank=True, default="",
        choices=[
            ("local_llama", "Local llama.cpp server"),
            ("local_vllm", "Local vLLM server"),
            ("cloud", "OpenAI-compatible cloud (MiniMax, Qwen, …)"),
        ],
        help_text="which dialect the outbound model speaks",
    )
    model_name = models.CharField(
        max_length=120, blank=True, default="",
        help_text="model id the provider should send in API calls",
    )
    model_base_url = models.CharField(
        max_length=300, blank=True, default="",
        help_text="where the model lives; provider appends /chat/completions",
    )
    model_api_key_cipher = models.TextField(
        blank=True, default="",
        help_text="encrypted Bearer token; plaintext is never stored",
    )
    model_max_tokens = models.IntegerField(null=True, blank=True)
    model_temperature = models.FloatField(null=True, blank=True)

    # --- model reachability, written by board.probe.probe_agent ---
    # A probe is a one-shot check, not a continuous heartbeat, so its result
    # is "still good" for a generous window. Storing the error too means the
    # admin can show *why* a model went unreachable instead of a generic
    # "no". Both fields default to null/blank — a row that was never probed
    # is distinct from one that probed and failed.
    model_checked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    model_check_ok = models.BooleanField(default=False)
    model_check_error = models.CharField(max_length=300, blank=True, default="")

    # Agent daemon heartbeat (last_seen_at) is a different signal — it shows
    # that an agent process checked in. A model-backed agent that is its own
    # process never sets it; a worker daemon that fronts a remote model sets
    # both. The two columns stay independent.
    REACHABLE_WINDOW_SECONDS = 120
    MODEL_REACHABLE_WINDOW_SECONDS = 24 * 3600  # probe is a one-shot test

    class Meta:
        db_table = "agents"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} [{self.status}]"

    @property
    def status(self) -> str:
        """
        Derived, not stored.

        A status column would go stale the instant an agent stopped checking in,
        and a stale 'reachable' is worse than no status at all.

        Prefers the most recent of the two signals: if a probe just succeeded
        OR a daemon just checked in, the row is reachable.
        """
        now = timezone.now()
        candidates = []
        if self.last_seen_at is not None:
            age = (now - self.last_seen_at).total_seconds()
            if age <= self.REACHABLE_WINDOW_SECONDS:
                candidates.append("daemon")
        if self.model_checked_at is not None and self.model_check_ok:
            age = (now - self.model_checked_at).total_seconds()
            if age <= self.MODEL_REACHABLE_WINDOW_SECONDS:
                candidates.append("model")
        if candidates:
            return self.Status.REACHABLE
        return self.Status.UNREACHABLE

    @property
    def is_reachable(self) -> bool:
        return self.status == self.Status.REACHABLE

    @property
    def model_reachable(self) -> bool:
        """True iff a recent probe succeeded."""
        if self.model_checked_at is None or not self.model_check_ok:
            return False
        age = (timezone.now() - self.model_checked_at).total_seconds()
        return age <= self.MODEL_REACHABLE_WINDOW_SECONDS

    @property
    def has_model(self) -> bool:
        """True when this row has enough info to dispatch a prompt to a model."""
        return bool(self.model_provider and self.model_base_url)

    def model_api_key(self) -> str:
        """Plaintext API key, decrypted. Empty if not set."""
        from .crypto import decrypt_secret
        return decrypt_secret(self.model_api_key_cipher) if self.model_api_key_cipher else ""

    def set_model_api_key(self, plaintext: str) -> None:
        """Encrypts and stores the key. Plaintext is never persisted."""
        from .crypto import encrypt_secret
        self.model_api_key_cipher = encrypt_secret(plaintext) if plaintext else ""

    def provider(self):
        """Build the AgentProvider this row declares, or raise ProviderError."""
        from providers.base import (
            CloudProvider, LocalLlamaProvider, ProviderError,
        )
        if not self.has_model:
            raise ProviderError(f"agent {self.name!r} has no model configured")
        key = self.model_api_key()
        common = dict(
            base_url=self.model_base_url,
            api_key=key,
            default_model=self.model_name,
        )
        if self.model_provider == "local_llama":
            return LocalLlamaProvider(**common)
        if self.model_provider == "local_vllm":
            from providers.base import LocalVLLMProvider
            return LocalVLLMProvider(**common)
        if self.model_provider == "cloud":
            return CloudProvider(
                **common, name=f"cloud:{self.name}",
            )
        raise ProviderError(f"unknown model_provider {self.model_provider!r}")

    def clean(self):
        super().clean()
        errors = {}
        if self.model_provider and not self.model_base_url:
            errors["model_base_url"] = (
                "model provider is set but base_url is empty"
            )
        if self.model_base_url and not self.model_provider:
            errors["model_provider"] = (
                "base_url is set but no provider is selected"
            )
        if errors:
            from django.core.exceptions import ValidationError
            raise ValidationError(errors)

    def heartbeat(self) -> None:
        Agent.objects.filter(pk=self.pk).update(last_seen_at=timezone.now())


class Backlog(models.Model):
    """A named container of tasks inside a project. One default per project."""

    id = models.CharField(primary_key=True, max_length=32, default=_uuid, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="backlogs"
    )
    name = models.CharField(max_length=200, default="Backlog")
    is_default = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "backlogs"
        ordering = ["project_id", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["project"], condition=models.Q(is_default=True),
                name="uniq_default_backlog_per_project",
            )
        ]

    def __str__(self) -> str:
        return f"{self.project.key}/{self.name}"


class ChatScope(models.TextChoices):
    PROJECTS = "projects", "Projects"
    BACKLOG = "backlog", "Backlog"
    TASK = "task", "Task"


class ChatSession(models.Model):
    """
    A conversation attached to one node of the tree.

    The scope decides what the agent on the other side may change and what it
    may read. That policy lives in board.permissions, not here — this model
    only records where the conversation is anchored.
    """

    id = models.CharField(primary_key=True, max_length=32, default=_uuid, editable=False)
    scope = models.CharField(max_length=16, choices=ChatScope.choices)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="chat_sessions"
    )
    backlog = models.ForeignKey(
        Backlog, on_delete=models.CASCADE, null=True, blank=True,
        related_name="chat_sessions",
    )
    task = models.ForeignKey(
        "Task", on_delete=models.CASCADE, null=True, blank=True,
        related_name="chat_sessions",
    )
    created_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "chat_sessions"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(scope=ChatScope.PROJECTS, backlog__isnull=True, task__isnull=True)
                    | models.Q(scope=ChatScope.BACKLOG, backlog__isnull=False, task__isnull=True)
                    | models.Q(scope=ChatScope.TASK, backlog__isnull=False, task__isnull=False)
                ),
                name="chat_scope_anchor_matches",
            )
        ]

    def __str__(self) -> str:
        return f"{self.scope}:{self.anchor_label()}"

    def anchor_label(self) -> str:
        if self.scope == ChatScope.TASK and self.task_id:
            return self.task.title[:50]
        if self.scope == ChatScope.BACKLOG and self.backlog_id:
            return f"{self.project.key}/{self.backlog.name}"
        return f"{self.project.key} (projects)"


class ChatMessage(models.Model):
    id = models.CharField(primary_key=True, max_length=32, default=_uuid, editable=False)
    session = models.ForeignKey(
        ChatSession, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(
        max_length=16,
        choices=[("user", "User"), ("agent", "Agent"), ("system", "System")],
    )
    agent = models.ForeignKey(
        Agent, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    content = models.TextField()
    tool_calls = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "chat_messages"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.role}: {self.content[:50]}"


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
#
# A Resource is a system-wide, finite thing that a task or project can claim.
# Six concrete kinds; one parent table; multi-table inheritance so a single
# FK can refer to any of them without discriminator-tag games.
#
# Resources are defined globally — there is no project_id on Resource. A
# resource either exists for the whole fleet or doesn't. Visibility / access
# to a resource is gated by what holds an allocation, not by who registered
# it; the operator owns registration.
#
# Persistent resources exist forever and have capacity 1: the host is THE host,
# the LLM endpoint is THE endpoint. Ephemeral resources are templates — they
# have a pool size, and each allocation spawns an instance from the template.
# The instance_id on ResourceAllocation is what links a granted slot to the
# concrete thing the agent talks to.


def _new_resource_id() -> str:
    return _uuid()


class ResourceKind(models.TextChoices):
    HOST = "host", "Host (physical or VM)"
    SANDBOX = "sandbox", "Docker sandbox"
    LLM_ENDPOINT = "llm_endpoint", "LLM endpoint"
    COMFYUI = "comfyui", "ComfyUI endpoint"
    GIT_REPO = "git_repo", "Git repository"
    BROWSER = "browser", "Web browser (Playwright)"


class ResourceLifetime(models.TextChoices):
    PERSISTENT = "persistent", "Persistent — exists always"
    EPHEMERAL = "ephemeral", "Ephemeral — spawned on allocation"


class Resource(models.Model):
    """
    Parent row for every concrete resource. Concrete children (Host, Sandbox,
    …) live in their own tables joined on the primary key.

    The parent exists so ResourceAllocation can FK to a single table and
    query "every resource, regardless of kind" without a UNION.
    """

    id = models.CharField(
        primary_key=True, max_length=32,
        default=_new_resource_id, editable=False,
    )
    name = models.CharField(max_length=80, unique=True)
    kind = models.CharField(max_length=24, choices=ResourceKind.choices)
    lifetime = models.CharField(
        max_length=16,
        choices=ResourceLifetime.choices,
        default=ResourceLifetime.PERSISTENT,
    )
    description = models.TextField(blank=True, default="")
    # Max concurrent allocations. 1 for most persistent kinds; >1 for
    # ephemeral pools (e.g. 4 browser containers).
    capacity = models.PositiveIntegerField(default=1)
    # When False the resource is hidden from new allocations but kept for
    # historical reference. We do not hard-delete resources that any
    # allocation ever pointed at.
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "resources"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} [{self.kind}]"

    @property
    def is_persistent(self) -> bool:
        return self.lifetime == ResourceLifetime.PERSISTENT

    @property
    def is_ephemeral(self) -> bool:
        return self.lifetime == ResourceLifetime.EPHEMERAL

    @property
    def active_allocations(self):
        return self.allocations.filter(status=ResourceAllocation.Status.GRANTED)

    @property
    def free_slots(self) -> int:
        used = self.active_allocations.count()
        return max(0, self.capacity - used)

    @property
    def is_full(self) -> bool:
        return self.free_slots == 0

    def set_secret(self, field_name: str, plaintext: str) -> None:
        """Encrypt and store a secret on one of the resource's credential fields.

        Allowed field names are exactly the *_cipher attributes on the
        concrete children; passing anything else raises ValueError so a typo
        cannot silently store plaintext.
        """
        from .crypto import encrypt_secret
        if not field_name.endswith("_cipher"):
            raise ValueError(f"refusing to write plaintext to {field_name!r}")
        if not hasattr(self, field_name):
            raise ValueError(f"no such field: {field_name}")
        setattr(self, field_name, encrypt_secret(plaintext) if plaintext else "")

    def get_secret(self, field_name: str) -> str:
        from .crypto import decrypt_secret
        if not field_name.endswith("_cipher"):
            raise ValueError(f"refusing to read plaintext from {field_name!r}")
        if not hasattr(self, field_name):
            raise ValueError(f"no such field: {field_name}")
        cipher = getattr(self, field_name) or ""
        return decrypt_secret(cipher)


# ---------------------------------------------------------------------------
# Concrete resource kinds — each in its own table joined to Resource on pk.
# ---------------------------------------------------------------------------


class Host(Resource):
    """
    A physical or virtual machine the agent can SSH into.

    Credentials are stored encrypted: either an SSH key (preferred) OR a
    login/password pair, never both.
    """

    os_type = models.CharField(
        max_length=16,
        choices=[("linux", "Linux"), ("windows", "Windows")],
        default="linux",
    )
    hostname = models.CharField(max_length=200, blank=True, default="")
    ip = models.CharField(max_length=64, blank=True, default="")
    proto = models.CharField(
        max_length=16,
        choices=[("ssh", "SSH"), ("winrm", "WinRM"), ("rdp", "RDP")],
        default="ssh",
    )
    port = models.PositiveIntegerField(null=True, blank=True)
    ssh_key_cipher = models.TextField(blank=True, default="")
    login = models.CharField(max_length=80, blank=True, default="")
    password_cipher = models.TextField(blank=True, default="")

    class Meta:
        db_table = "resources_host"

    def __str__(self) -> str:
        target = self.ip or self.hostname or "(no target)"
        return f"host:{self.name} → {target}"

    def save(self, *args, **kwargs):
        self.kind = ResourceKind.HOST
        return super().save(*args, **kwargs)


class Sandbox(Resource):
    """
    A Docker container spawned from an image, freed when released.

    The image, CPU/memory limits and connection template describe what
    gets created when an allocation is granted. The actual docker
    plumbing is the runtime's job; the data here is the contract.
    """

    os_type = models.CharField(
        max_length=16,
        choices=[("linux", "Linux"), ("windows", "Windows")],
        default="linux",
    )
    image = models.CharField(max_length=200, default="ubuntu:22.04")
    cpu_limit = models.FloatField(null=True, blank=True)
    memory_limit_mb = models.PositiveIntegerField(null=True, blank=True)
    # Connection details for the spawned container — same shape as Host.
    hostname = models.CharField(max_length=200, blank=True, default="")
    ip = models.CharField(max_length=64, blank=True, default="")
    proto = models.CharField(max_length=16, default="ssh")
    port = models.PositiveIntegerField(null=True, blank=True)
    ssh_key_cipher = models.TextField(blank=True, default="")
    login = models.CharField(max_length=80, blank=True, default="")
    password_cipher = models.TextField(blank=True, default="")
    # Pool size — how many containers can run at once. Defaults to capacity
    # at creation but kept as its own column so the two meanings do not blur.
    pool_size = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "resources_sandbox"

    def save(self, *args, **kwargs):
        # capacity == pool_size for ephemeral sandboxes; surface that as a
        # single field the operator thinks about, not two they must keep in sync.
        # pool_size is a PositiveIntegerField with default 1, so the copy is
        # unconditional.
        self.capacity = self.pool_size
        self.kind = ResourceKind.SANDBOX
        self.lifetime = ResourceLifetime.EPHEMERAL
        return super().save(*args, **kwargs)


class LlmEndpoint(Resource):
    """An OpenAI-compatible model server: base URL + model id + Bearer key."""

    base_url = models.CharField(max_length=300)
    model_id = models.CharField(max_length=200, blank=True, default="")
    api_key_cipher = models.TextField(blank=True, default="")
    max_tokens = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "resources_llm_endpoint"

    def __str__(self) -> str:
        return f"llm:{self.name} → {self.base_url}"

    def save(self, *args, **kwargs):
        self.kind = ResourceKind.LLM_ENDPOINT
        return super().save(*args, **kwargs)


class ComfyUiEndpoint(Resource):
    """A ComfyUI HTTP server reachable at url. Optional API key."""

    url = models.CharField(max_length=300)
    api_key_cipher = models.TextField(blank=True, default="")
    workflow_timeout_seconds = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "resources_comfyui"

    def __str__(self) -> str:
        return f"comfyui:{self.name} → {self.url}"

    def save(self, *args, **kwargs):
        self.kind = ResourceKind.COMFYUI
        return super().save(*args, **kwargs)


class GitRepo(Resource):
    """A git repository the agent can clone. SSH key is optional."""

    url = models.CharField(max_length=500)
    default_branch = models.CharField(max_length=120, blank=True, default="main")
    ssh_key_cipher = models.TextField(blank=True, default="")

    class Meta:
        db_table = "resources_git_repo"

    def __str__(self) -> str:
        return f"git:{self.name} → {self.url}"

    def save(self, *args, **kwargs):
        self.kind = ResourceKind.GIT_REPO
        return super().save(*args, **kwargs)


class Browser(Resource):
    """
    A Playwright-driven web browser. Always ephemeral — every allocation
    gets its own container.
    """

    image = models.CharField(
        max_length=200,
        default="mcr.microsoft.com/playwright:v1.48.0-jammy",
    )
    headless = models.BooleanField(default=True)
    pool_size = models.PositiveIntegerField(default=2)

    class Meta:
        db_table = "resources_browser"

    def save(self, *args, **kwargs):
        self.capacity = self.pool_size
        # Browsers are always ephemeral; refuse a persistent override.
        self.lifetime = ResourceLifetime.EPHEMERAL
        self.kind = ResourceKind.BROWSER
        return super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Allocation — a task or project's claim on a resource
# ---------------------------------------------------------------------------


class ResourceAllocation(models.Model):
    """
    A task or project's claim on a resource.

    Either task OR project is set (not both, not neither). The constraint is
    enforced in clean(); the schema is permissive because Django can't
    express XOR constraints. Agent is optional metadata — the agent who
    asked for the allocation, if it was a model-driven request.
    """

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        GRANTED = "granted", "Granted"
        RELEASED = "released", "Released"
        FAILED = "failed", "Failed"

    id = models.CharField(
        primary_key=True, max_length=32,
        default=_new_resource_id, editable=False,
    )
    resource = models.ForeignKey(
        Resource, on_delete=models.PROTECT, related_name="allocations",
    )
    task = models.ForeignKey(
        "Task", on_delete=models.CASCADE,
        null=True, blank=True, related_name="resource_allocations",
    )
    project = models.ForeignKey(
        "Project", on_delete=models.CASCADE,
        null=True, blank=True, related_name="resource_allocations",
    )
    agent = models.ForeignKey(
        "Agent", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="resource_allocations",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.REQUESTED,
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    granted_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    # For ephemeral resources: the spawned instance. For Docker that's a
    # container ID; for a browser it's a session id; for persistent
    # resources it stays empty.
    instance_id = models.CharField(max_length=200, blank=True, default="")
    instance_endpoint = models.CharField(max_length=300, blank=True, default="")
    error = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "resource_allocations"
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["resource", "status"]),
        ]

    def __str__(self) -> str:
        owner = self.task_id or self.project_id or "-"
        return f"{self.resource_id} → {owner} [{self.status}]"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.GRANTED

    def clean(self):
        super().clean()
        errors = {}
        # Exactly one of (task, project) must be set. Both empty leaves the
        # allocation orphaned; both set is ambiguous — the agent cannot tell
        # whether this is a per-task override or a project-level grant.
        if bool(self.task_id) == bool(self.project_id):
            errors["__all__"] = (
                "exactly one of task or project must be set"
            )
        if errors:
            from django.core.exceptions import ValidationError
            raise ValidationError(errors)
