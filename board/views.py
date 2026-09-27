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
    columns = []
    by_status = (
        Task.objects.values("status")
        .annotate(n=Count("id"))
        .order_by()
    )
    counts = {row["status"]: row["n"] for row in by_status}
    for st in Status:
        columns.append(
            {
                "status": st.value,
                "label": st.label,
                "count": counts.get(st.value, 0),
                "tasks": Task.objects.filter(status=st.value).order_by(
                    "-priority", "created_at"
                )[:50],
            }
        )
    from .permissions import visible_projects

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
    task = get_object_or_404(Task, pk=task_id)
    return render(
        request,
        "board/task_detail.html",
        {"task": task, "events": task.events.order_by("-ts")[:100]},
    )


@login_required
def reports(request):
    from .models import TaskEvent

    return render(
        request,
        "board/reports.html",
        {
            "by_status": list(
                Task.objects.values("status").annotate(n=Count("id")).order_by("-n")
            ),
            "by_actor": list(
                TaskEvent.objects.values("actor").annotate(n=Count("id")).order_by("-n")
            ),
            "by_kind": list(
                Task.objects.values("kind").annotate(n=Count("id")).order_by("-n")
            ),
            "total_tokens": sum(
                Task.objects.values_list("tokens_used", flat=True)
            ),
            "total_attempts": sum(
                Task.objects.values_list("attempts", flat=True)
            ),
            "stuck": list(
                Task.objects.filter(stuck_score__gt=0)
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
