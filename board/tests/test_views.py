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


# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------
#
# The board has no registration. Accounts are created by the operator on the
# host, and every board view requires a session — the operator controls the
# process, and anyone on the LAN must not be able to move cards.


@pytest.mark.django_db
def test_board_requires_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login/" in r["Location"]


@pytest.mark.django_db
def test_task_detail_requires_login(client):
    t = Task.objects.create(title="secret", acceptance="x", status=Status.READY)
    r = client.get(f"/task/{t.pk}/")
    assert r.status_code == 302
    assert "/login/" in r["Location"]


@pytest.mark.django_db
def test_reports_requires_login(client):
    r = client.get("/reports/")
    assert r.status_code == 302
    assert "/login/" in r["Location"]


@pytest.mark.django_db
def test_healthz_stays_open_for_probes(client):
    r = client.get("/healthz")
    assert r.status_code == 200


@pytest.mark.django_db
def test_login_page_is_public(client):
    r = client.get("/login/")
    assert r.status_code == 200
    assert "csrfmiddlewaretoken" in r.content.decode()


@pytest.mark.django_db
def test_successful_login_grants_board_access(client, django_user_model):
    u = django_user_model.objects.create_user("op", password="pw12345")
    r = client.post(
        "/login/", {"username": "op", "password": "pw12345"}, follow=True
    )
    assert r.status_code == 200
    assert "Ready" in r.content.decode()
    # session cookie is issued so the operator is not asked again
    assert client.session.get("_auth_user_id") == str(u.pk)


@pytest.mark.django_db
def test_bad_credentials_do_not_authenticate(client, django_user_model):
    django_user_model.objects.create_user("op", password="pw12345")
    r = client.post("/login/", {"username": "op", "password": "wrong"})
    assert r.status_code == 200
    assert client.session.get("_auth_user_id") is None


@pytest.mark.django_db
def test_logout_ends_the_session(client, django_user_model):
    django_user_model.objects.create_user("op", password="pw12345")
    client.post("/login/", {"username": "op", "password": "pw12345"})
    assert client.session.get("_auth_user_id") is not None
    client.post("/logout/")
    assert client.session.get("_auth_user_id") is None
    r = client.get("/")
    assert r.status_code == 302


@pytest.mark.django_db
def test_there_is_no_registration_path(client):
    """No signup, no password reset — accounts are operator-created only."""
    for path in ("/register/", "/signup/", "/accounts/signup/", "/password_reset/"):
        assert client.get(path).status_code == 404, path
