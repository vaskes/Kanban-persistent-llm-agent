"""Minimal views for phase 0. The full HTMX board lands in phase 1."""

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from . import permissions
from .models import Project, ProjectMembership, Status, Task

# Every board view requires a session. There is no registration: accounts are
# created by the operator, and only on this host. /healthz is the sole
# exemption so a liveness probe still works.


@login_required
def board(request):
    """
    The board is per-project. The caller picks a project with ?project=<key>;
    without one we pick the first project the user is allowed to see. An
    account with no project access sees no cards — not a header above a list
    of someone else's work, which is what the old mixed-board view rendered.
    """

    visible_qs = permissions.tasks_visible_to(request.user)
    visible_projects_qs = list(permissions.visible_projects(request.user))

    current = None
    requested = request.GET.get("project", "").strip()
    if requested:
        for p in visible_projects_qs:
            if p.key == requested:
                current = p
                break
    if current is None and visible_projects_qs:
        current = visible_projects_qs[0]

    if current is not None:
        visible = visible_qs.filter(project=current)
    else:
        visible = visible_qs.none()

    counts = {
        row["status"]: row["n"]
        for row in visible.values("status").annotate(n=Count("id"))
    }
    columns = [
        {
            "status": st.value,
            "label": st.label,
            "count": counts.get(st.value, 0),
            "tasks": visible.filter(status=st.value).order_by(
                "-priority", "created_at"
            )[:50],
        }
        for st in Status
    ]

    # Per-tab task counters come from the same scoped query, one round-trip per
    # project — fine at this scale, and keeps the tab badge truthful (a card
    # in a project you cannot see must never appear in another project's tab).
    project_ids = [p.id for p in visible_projects_qs]
    per_project_counts = {
        row["project_id"]: row["n"]
        for row in Task.objects.filter(project_id__in=project_ids)
        .values("project_id")
        .annotate(n=Count("id"))
    }
    tabs = [
        {
            "key": p.key,
            "name": p.name,
            "count": per_project_counts.get(p.id, 0),
            "is_default": p.is_default,
            "archived": p.archived,
            "active": current is not None and p.key == current.key,
        }
        for p in visible_projects_qs
    ]

    return render(
        request,
        "board/board.html",
        {
            "columns": columns,
            "tabs": tabs,
            "current": current,
            "is_admin": permissions.user_may_create_projects(request.user),
            "visible_total": visible.count(),
            "members_count": (
                current.memberships.count() if current is not None else 0
            ),
            "empty_in_current": (
                current is not None and not visible.exists()
            ),
            # "No access yet" and "an empty board" look identical otherwise,
            # and the operator would have to guess which one they are looking at.
            "no_projects": not visible_projects_qs,
        },
    )


@login_required
def task_detail(request, task_id):
    """
    Scoped lookup, not get_object_or_404 on the raw id.

    Any authenticated user could previously read any card by guessing its id,
    including the goal, the acceptance criteria and the full audit trail.
    """

    task = get_object_or_404(permissions.tasks_visible_to(request.user), pk=task_id)
    return render(
        request,
        "board/task_detail.html",
        {"task": task, "events": task.events.order_by("-ts")[:100]},
    )


@login_required
def reports(request):
    from .models import TaskEvent

    visible = permissions.tasks_visible_to(request.user)
    # events follow their tasks: an event about an invisible card is itself
    # information about a card the reader is not allowed to see
    visible_ids = visible.values_list("id", flat=True)

    return render(
        request,
        "board/reports.html",
        {
            "by_status": list(
                visible.values("status").annotate(n=Count("id")).order_by("-n")
            ),
            "by_actor": list(
                TaskEvent.objects.filter(task_id__in=visible_ids)
                .values("actor")
                .annotate(n=Count("id"))
                .order_by("-n")
            ),
            "by_kind": list(
                visible.values("kind").annotate(n=Count("id")).order_by("-n")
            ),
            "total_tokens": sum(visible.values_list("tokens_used", flat=True)),
            "total_attempts": sum(visible.values_list("attempts", flat=True)),
            "stuck": list(
                visible.filter(stuck_score__gt=0)
                .order_by("-stuck_score")
                .values("id", "title", "stuck_score")[:20]
            ),
        },
    )


def register(request):
    """
    Self-service account creation.

    A new account sees nothing: no project, no tasks, no default project. It
    becomes useful only once an administrator grants read access to something.
    Saying so on the page is better than showing an empty board and leaving
    the user to wonder whether they did something wrong.
    """
    from .forms import RegistrationForm, new_user_sees_nothing, register_user

    if request.user.is_authenticated:
        return redirect("board")

    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user, is_first = register_user(form)
            login(request, user)
            messages.success(
                request,
                "Account created as an administrator. You can see every project."
                if is_first
                else "Account created. You will see something once an "
                     "administrator grants you access to a project.",
            )
            return redirect("board")
    else:
        form = RegistrationForm()

    return render(
        request,
        "board/register.html",
        {"form": form, "sess_sees_nothing": new_user_sees_nothing},
    )


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


def _projects_context(request):

    return {"may_create": permissions.user_may_create_projects(request.user)}


@login_required
def projects_list(request):

    visible = permissions.visible_projects(request.user).select_related("created_by")
    rows = []
    for p in visible:
        rows.append(
            {
                "project": p,
                "task_count": p.tasks.count(),
                "member_count": p.memberships.count(),
                "can_write": permissions.user_can_write_project(request.user, p),
                "backlogs": p.backlogs.all(),
            }
        )
    return render(
        request,
        "board/projects.html",
        {"rows": rows, **_projects_context(request)},
    )


@login_required
def project_create(request):

    if not permissions.user_may_create_projects(request.user):
        # Creating projects is an administrator action by design. A 403 with a
        # page beats a hidden button that leaves people wondering.
        return render(
            request,
            "board/refused.html",
            {
                "title": "Not allowed",
                "detail": "Only an administrator can create projects. "
                          "Ask one to make you an administrator, or ask for "
                          "access to an existing project.",
            },
            status=403,
        )

    from .project_forms import ProjectForm

    form = ProjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        project = form.save(commit=False)
        project.key = _slugify(project.name)
        base, n = project.key, 2
        while Project.objects.filter(key=project.key).exists():
            project.key = f"{base}-{n}"
            n += 1
        project.created_by = request.user
        project.save()
        _ensure_backlog(project)
        messages.success(request, f"Project '{project.name}' created.")
        return redirect("project_detail", key=project.key)

    return render(
        request,
        "board/project_form.html",
        {"form": form, "mode": "create", **_projects_context(request)},
    )


@login_required
def project_detail(request, key):

    project = _visible_project(request.user, key)
    if project is None:
        return _no_access(request, key)

    can_write = permissions.user_can_write_project(request.user, project)
    return render(
        request,
        "board/project_detail.html",
        {
            "project": project,
            "backlogs": project.backlogs.all(),
            "members": project.memberships.select_related("user"),
            "can_write": can_write,
            "can_manage": permissions.user_may_create_projects(request.user),
            "tasks": project.tasks.order_by("-priority", "created_at")[:100],
            "task_count": project.tasks.count(),
            **_projects_context(request),
        },
    )


@login_required
def project_edit(request, key):

    project = _visible_project(request.user, key)
    if project is None:
        return _no_access(request, key)
    if not permissions.user_can_write_project(request.user, project):
        return _no_write(request, project)

    from .project_forms import ProjectForm

    form = ProjectForm(request.POST or None, instance=project)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Project saved.")
        return redirect("project_detail", key=project.key)

    return render(
        request,
        "board/project_form.html",
        {
            "form": form,
            "mode": "edit",
            "project": project,
            **_projects_context(request),
        },
    )


@login_required
def project_delete(request, key):

    project = _visible_project(request.user, key)
    if project is None:
        return _no_access(request, key)
    if not permissions.user_may_create_projects(request.user):
        return _no_write(request, project)

    if request.method != "POST":
        return render(
            request,
            "board/confirm_delete.html",
            {"project": project, **_projects_context(request)},
        )

    if project.is_default:
        messages.error(
            request,
            "The default project cannot be deleted. It is this repository, and "
            "the whole task tree hangs from it. Archive it instead if needed.",
        )
        return redirect("project_detail", key=project.key)

    name = project.name
    project.delete()
    messages.success(request, f"Project '{name}' deleted.")
    return redirect("projects_list")


@login_required
def project_members(request, key):

    project = _visible_project(request.user, key)
    if project is None:
        return _no_access(request, key)
    if not permissions.user_may_create_projects(request.user):
        return _no_write(request, project)

    from .project_forms import MembershipForm

    form = MembershipForm(request.POST or None, project=project)
    if request.method == "POST" and form.is_valid():
        ProjectMembership.objects.create(
            project=project,
            user=form.cleaned_data["user"],
            can_write=form.cleaned_data["can_write"],
            granted_by=request.user,
        )
        messages.success(
            request,
            f"{form.cleaned_data['user'].username} now has "
            f"{'write' if form.cleaned_data['can_write'] else 'read'} access.",
        )
        return redirect("project_members", key=project.key)

    return render(
        request,
        "board/members.html",
        {
            "project": project,
            "form": form,
            "members": project.memberships.select_related("user", "granted_by"),
            "creator": project.created_by,
            **_projects_context(request),
        },
    )


@login_required
def member_revoke(request, key, user_id):

    project = _visible_project(request.user, key)
    if project is None:
        return _no_access(request, key)
    if not permissions.user_may_create_projects(request.user):
        return _no_write(request, project)
    if request.method != "POST":
        return redirect("project_members", key=project.key)

    m = project.memberships.filter(user_id=user_id).first()
    if m is None:
        return redirect("project_members", key=project.key)
    name = m.user.username
    m.delete()
    messages.success(request, f"{name} no longer has access to {project.key}.")
    return redirect("project_members", key=project.key)


@login_required
def member_toggle(request, key, user_id):
    """Flip an existing member between read and write."""

    project = _visible_project(request.user, key)
    if project is None:
        return _no_access(request, key)
    if not permissions.user_may_create_projects(request.user):
        return _no_write(request, project)
    if request.method != "POST":
        return redirect("project_members", key=project.key)

    m = project.memberships.filter(user_id=user_id).first()
    if m is None:
        return redirect("project_members", key=project.key)
    m.can_write = not m.can_write
    m.save(update_fields=["can_write"])
    verb = "write" if m.can_write else "read"
    messages.success(request, f"{m.user.username} now has {verb} access.")
    return redirect("project_members", key=project.key)


# --- helpers ---------------------------------------------------------------


def _slugify(name: str) -> str:
    from django.utils.text import slugify

    base = slugify(name)[:60] or "project"
    return base


def _ensure_backlog(project):
    from .models import Backlog

    if not project.backlogs.filter(is_default=True).exists():
        Backlog.objects.create(project=project, name="Backlog", is_default=True)


def _visible_project(user, key):

    p = Project.objects.filter(key=key).first()
    if p is None or not permissions.user_can_see_project(user, p):
        return None
    return p


def _no_access(request, key):
    """
    404 for a project you may not see.

    Not 403: whether a project exists is itself information a user with no
    access to it should not be able to enumerate.
    """
    from django.http import Http404

    raise Http404("no such project")


def _no_write(request, project):
    return render(
        request,
        "board/refused.html",
        {
            "title": "Read-only",
            "detail": f"You have read access to '{project.key}' but not write "
                      f"access. Ask an administrator to change it.",
        },
        status=403,
    )
