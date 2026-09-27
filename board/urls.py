from django.contrib.auth import views as auth_views
from django.http import JsonResponse
from django.urls import path

from . import views


def healthz(_request):
    # Deliberately unauthenticated: a liveness probe cannot log in.
    return JsonResponse({"ok": True, "service": "kanban-agent"})


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("login/", auth_views.LoginView.as_view(template_name="board/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.board, name="board"),
    path("task/<str:task_id>/", views.task_detail, name="task_detail"),
    path("reports/", views.reports, name="reports"),
]
