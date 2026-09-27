"""Minimal views for phase 0. The full HTMX board lands in phase 1."""

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from .models import Status, Task

# Every board view requires a session. There is no registration: accounts are
# created by the operator, and only on this host. /healthz is the sole
# exemption so a liveness probe still works.


@login_required
def board(request):
    """
    The board shows only tasks whose project the user may see.

    Filtering here rather than trusting the caller is the whole point. An
    account with no project access must see no cards — not a "no access"
    banner sitting above a list of everyone else's work, which is what this
    used to render.
    """
    from .permissions import tasks_visible_to, visible_projects

    visible = tasks_visible_to(request.user)
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

    return render(
        request,
        "board/board.html",
        {
            "columns": columns,
            # "No access yet" and "an empty board" look identical otherwise,
            # and the operator would have to guess which one they are looking at.
            "empty": not visible_projects(request.user).exists(),
        },
    )


@login_required
def task_detail(request, task_id):
    """
    Scoped lookup, not get_object_or_404 on the raw id.

    Any authenticated user could previously read any card by guessing its id,
    including the goal, the acceptance criteria and the full audit trail.
    """
    from .permissions import tasks_visible_to

    task = get_object_or_404(tasks_visible_to(request.user), pk=task_id)
    return render(
        request,
        "board/task_detail.html",
        {"task": task, "events": task.events.order_by("-ts")[:100]},
    )


@login_required
def reports(request):
    from .models import TaskEvent
    from .permissions import tasks_visible_to

    visible = tasks_visible_to(request.user)
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
