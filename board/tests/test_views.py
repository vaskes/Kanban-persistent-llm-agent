"""
Smoke tests for the HTTP surface.

These exist because the unit tests exercise models and services only. A view
can 500 while every unit test stays green — which is exactly what happened with
/reports/ — so the URLs need their own coverage.
"""

import pytest
from django.urls import reverse

from board.models import Status, Task

pytestmark = pytest.mark.django_db


def test_healthz(client):
    r = client.get(reverse("board:healthz") if False else "/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_board_renders_all_columns(client):
    Task.objects.create(title="visible", acceptance="x", status=Status.READY)
    r = client.get("/")
    assert r.status_code == 200
    body = r.content.decode()
    for label in ("Inbox", "Backlog", "Ready", "In progress", "Review", "Done"):
        assert label in body, f"missing column {label}"
    assert "visible" in body


def test_task_detail_renders_audit_trail(client):
    t = Task.objects.create(title="audited", acceptance="x", status=Status.READY)
    from board.models import Actor
    from board.state import Ctx, transition

    transition(t, Status.IN_PROGRESS, Ctx(actor=Actor.AGENT, reason="claimed for test"))
    r = client.get(f"/task/{t.pk}/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "audited" in body
    assert "agent" in body


def test_reports_renders(client):
    Task.objects.create(title="token burner", acceptance="x", status=Status.READY, tokens_used=1234)
    r = client.get("/reports/")
    assert r.status_code == 200
    assert "reports" in r.content.decode()


def test_unknown_task_returns_404(client):
    assert client.get("/task/doesnotexist/").status_code == 404
