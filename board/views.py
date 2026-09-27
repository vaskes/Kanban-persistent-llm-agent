"""Minimal views for phase 0. The full HTMX board lands in phase 1."""

from django.db.models import Count
from django.shortcuts import get_object_or_404, render

from .models import Status, Task


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


def task_detail(request, task_id):
    task = get_object_or_404(Task, pk=task_id)
    return render(
        request,
        "board/task_detail.html",
        {"task": task, "events": task.events.order_by("-ts")[:100]},
    )


def reports(request):
    from .models import Actor

    return render(
        request,
        "board/reports.html",
        {
            "by_status": list(
                Task.objects.values("status").annotate(n=Count("id")).order_by("-n")
            ),
            "by_actor": list(
                Task.events.values("actor").annotate(n=Count("id")).order_by("-n")
            ),
            "total_tokens": sum(
                Task.objects.values_list("tokens_used", flat=True)
            ),
        },
    )
