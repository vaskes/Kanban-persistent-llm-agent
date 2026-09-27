from django.urls import path
from django.http import JsonResponse

from . import views


def healthz(_request):
    return JsonResponse({"ok": True, "service": "kanban-agent"})


urlpatterns = [
    path("healthz", healthz),
    path("", views.board, name="board"),
    path("task/<str:task_id>/", views.task_detail, name="task_detail"),
    path("reports/", views.reports, name="reports"),
]
