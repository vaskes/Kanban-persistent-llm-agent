"""Minimal views for phase 0. The full HTMX board lands in phase 1."""

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import get_object_or_404, render

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
    return render(request, "board/board.html", {"columns": columns})


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
