"""
Access policy for the project / backlog / task tree.

The tree is small on purpose: `Project -> Backlog -> Task`. What varies is
which level a conversation is anchored at, and therefore what the agent on the
other side is allowed to change.

    scope       may change              may read
    ---------   ---------------------   ---------------------------------
    projects    projects (create,       projects, their backlogs, their tasks
                delete, edit)
    backlog     tasks in this backlog   this project, the tasks in it
    task        this task               this task, its backlog, its project

The rule behind it: an agent changes the level it was invoked at, and reads
outward from there. Anchored at a task it can see how that task came to be;
anchored at the project it can see everything under it, because a project-level
conversation is by definition about the shape of the whole thing.

Read access to *data* (read a project, read tasks) is separate from read access
*up the tree* (see the parent). A task's agent can see that the task lives in a
backlog of a project without being able to change either.

Write permission never implies a sibling scope. A backlog agent cannot touch the
project, and a project agent cannot silently rewrite individual tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import ChatScope


class PermissionError_(Exception):
    """Raised when an action is outside the scope's grant."""


@dataclass(frozen=True)
class Grant:
    """What a conversation at one scope is permitted to do."""

    scope: str
    can_change_projects: bool = False
    can_change_backlogs: bool = False
    can_change_tasks: bool = False
    task_scoped: bool = False          # changes limited to the anchor task
    can_read_projects: bool = True
    can_read_backlogs: bool = True
    can_read_tasks: bool = True
    can_delete_projects: bool = False
    label: str = ""

    @property
    def can_change(self) -> str:
        """Human-readable summary, used in prompts and in the UI."""
        if self.scope == ChatScope.PROJECTS:
            return "projects"
        if self.scope == ChatScope.BACKLOG:
            return "tasks in this backlog"
        return "this task"

    def assert_can_change(self, kind: str, *, target_task_id: str | None = None,
                          anchor_task_id: str | None = None) -> None:
        if kind == "project":
            if not self.can_change_projects:
                raise PermissionError_(
                    "a conversation at this level may not change projects"
                )
            return
        if kind == "backlog":
            if not self.can_change_backlogs:
                raise PermissionError_(
                    "a conversation at this level may not change backlogs"
                )
            return
        if kind == "task":
            if not self.can_change_tasks:
                raise PermissionError_(
                    "a conversation at this level may not change tasks"
                )
            if self.task_scoped and target_task_id != anchor_task_id:
                raise PermissionError_(
                    "a task conversation may only change the task it is anchored to"
                )
            return
        raise PermissionError_(f"unknown resource kind: {kind}")

    def assert_can_read(self, kind: str) -> None:
        allowed = {
            "project": self.can_read_projects,
            "backlog": self.can_read_backlogs,
            "task": self.can_read_tasks,
        }.get(kind)
        if allowed is None:
            raise PermissionError_(f"unknown resource kind: {kind}")
        if not allowed:
            raise PermissionError_(
                f"a conversation at this level may not read {kind}s"
            )


GRANTS: dict[str, Grant] = {
    # Can create and delete projects, and read the whole tree beneath them.
    ChatScope.PROJECTS: Grant(
        scope=ChatScope.PROJECTS,
        can_change_projects=True,
        can_delete_projects=True,
        can_read_projects=True,
        can_read_backlogs=True,
        can_read_tasks=True,
    ),
    # Can change the tasks it is scoped to, and read the project above it.
    ChatScope.BACKLOG: Grant(
        scope=ChatScope.BACKLOG,
        can_change_tasks=True,
        can_read_projects=True,
        can_read_backlogs=True,
        can_read_tasks=True,
    ),
    # Can change only its own task. Reads upward: task -> backlog -> project.
    ChatScope.TASK: Grant(
        scope=ChatScope.TASK,
        can_change_tasks=True,
        task_scoped=True,
        can_read_projects=True,
        can_read_backlogs=True,
        can_read_tasks=True,
    ),
}


def grant_for(scope: str) -> Grant:
    try:
        return GRANTS[scope]
    except KeyError as exc:
        raise PermissionError_(f"unknown chat scope: {scope}") from exc


# ---------------------------------------------------------------------------
# user-level access to a project (as opposed to scope-level capability)
# ---------------------------------------------------------------------------


def user_can_see_project(user, project) -> bool:
    """
    Superusers and admins see everything. Otherwise: the creator, or anyone
    with a membership row.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    if project.created_by_id is not None and project.created_by_id == user.pk:
        return True
    return project.memberships.filter(user=user).exists()


def user_can_write_project(user, project) -> bool:
    """
    Write access to a project's contents. Membership carries the flag; admins
    and the creator always have it.
    """
    if not user_can_see_project(user, project):
        return False
    if user.is_superuser or user.is_staff:
        return True
    if project.created_by_id is not None and project.created_by_id == user.pk:
        return True
    m = project.memberships.filter(user=user).first()
    return bool(m and m.can_write)


def user_may_create_projects(user) -> bool:
    """
    Only admins create projects. Everyone else gets theirs by being granted a
    membership — which means project creation is not self-service, by design.
    """
    return bool(user and getattr(user, "is_authenticated", False)
                and (user.is_superuser or user.is_staff))


def visible_projects(user):
    """Projects the user may see, default project first."""
    from django.db.models import Q

    from .models import Project

    if not user or not getattr(user, "is_authenticated", False):
        return Project.objects.none()
    if user.is_superuser or user.is_staff:
        return Project.objects.all()
    return Project.objects.filter(
        Q(created_by=user) | Q(memberships__user=user)
    ).distinct()


def can_assign(agent) -> bool:
    """
    A task may name any registered agent, or none at all (meaning "any").

    "Registered" means a row actually exists. An unsaved Agent instance carries
    a primary key but references nothing, so accepting one would let a stale
    form point at an agent that does not exist — and the task would then be
    invisible to every worker, since `any` does not include a named agent.
    """
    if agent is None:
        return True
    if agent.pk is None:
        return False
    from .models import Agent

    return Agent.objects.filter(pk=agent.pk).exists()


# ---------------------------------------------------------------------------
# task visibility — the single place this is decided
# ---------------------------------------------------------------------------
#
# Everything that reads or writes a task goes through here. It used not to: the
# board listed every card, and a task could be fetched, claimed, heartbeaten,
# reviewed and released by its id alone. A freshly registered account with zero
# project access could therefore read and modify the entire system.
#
# The rule is deliberately simple — a task is visible when its project is
# visible — because anything subtler is a rule nobody can check by reading.


def is_admin(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False)
                and (user.is_superuser or user.is_staff))


def tasks_visible_to(user):
    """The tasks a user may read. Admins see everything."""
    from .models import Task

    if is_admin(user):
        return Task.objects.all()
    if not user or not getattr(user, "is_authenticated", False):
        return Task.objects.none()
    return Task.objects.filter(project__in=visible_projects(user))


def user_can_see_task(user, task) -> bool:
    if is_admin(user):
        return True
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if task.project_id is None:
        # An orphan task belongs to no project, so no membership can grant it.
        return False
    return user_can_see_project(user, task.project)


def visible_project_ids(user) -> list[str] | None:
    """
    Project ids a user may touch, or None when the restriction does not apply.

    None means "no restriction" rather than an empty list — passing an empty
    list into a filter must match nothing, and the two cases are easy to
    confuse. claim() needs the distinction.
    """
    if is_admin(user):
        return None
    if not user or not getattr(user, "is_authenticated", False):
        return []
    return list(visible_projects(user).values_list("id", flat=True))
