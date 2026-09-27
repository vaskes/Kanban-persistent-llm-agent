from django.contrib.auth import views as auth_views
from django.http import JsonResponse
from django.urls import path

from . import api, views


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

# --- agent API ---
# Bearer-token authenticated, session-free, csrf_exempt per-view (see board/api.py).
api_urlpatterns = [
    path("api/v1/me", api.me, name="api_me"),
    path("api/v1/tasks", api.list_tasks, name="api_list_tasks"),
    path("api/v1/board", api.board_state, name="api_board"),
    # ORDER MATTERS: the literal 'claim' segment must be declared before the
    # <str:task_id> pattern below, or 'claim' is captured as a task id and the
    # endpoint returns 405 instead of handling a POST.
    path("api/v1/tasks/claim", api.claim_task, name="api_claim"),
    path("api/v1/tasks/<str:task_id>", api.get_task, name="api_get_task"),
    path("api/v1/tasks/<str:task_id>/heartbeat", api.task_heartbeat, name="api_heartbeat"),
    path("api/v1/tasks/<str:task_id>/review", api.submit_review, name="api_review"),
    path("api/v1/tasks/<str:task_id>/release", api.release_task, name="api_release"),
    path("api/v1/tasks/<str:task_id>/escalate", api.escalate, name="api_escalate"),
]
urlpatterns += api_urlpatterns
