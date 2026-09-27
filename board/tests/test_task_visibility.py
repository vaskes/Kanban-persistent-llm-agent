"""
Task visibility across the web UI and the API.

This file exists because of a real defect, not for coverage. Registration was
added, a "no projects yet" banner was added, and the board was never
filtered — so a freshly registered account with *zero* project access was
shown every card in the system, could read any card by id, and could claim,
heartbeat, review, release and escalate any card through the API.

The invariant: a task is visible when its project is visible, and nothing else
decides it. Every read and write path goes through board.permissions.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from board.bootstrap import get_default_backlog, get_default_project
from board.management.commands.create_api_key import mint
from board.models import (
    AgentApiKey,
    Project,
    ProjectMembership,
    Status,
    Task,
)
from board.permissions import (
    tasks_visible_to,
    user_can_see_task,
    visible_project_ids,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def owner():
    """First account: an administrator."""
    return User.objects.create_user("owner", password="pw12345678")


_n = {"i": 0}


@pytest.fixture
def stranger():
    """
    A registered account with no membership anywhere.

    Self-contained: it creates its own throwaway bootstrap account first,
    because whichever account is created first on a fresh test database is
    promoted to administrator. It must not depend on the `owner` fixture also
    having run.
    """
    _n["i"] += 1
    User.objects.create_user(f"bootstrap{_n['i']}", password="pw12345678")
    u = User.objects.create_user("stranger", password="pw12345678")
    assert u.is_superuser is False
    return u


@pytest.fixture
def secret_task(owner):
    """A card in the default project, belonging to the owner."""
    p = get_default_project()
    b = get_default_backlog(p)
    return Task.objects.create(
        title="CLASSIFIED PLAN",
        goal="the private goal",
        acceptance="classified acceptance",
        status=Status.READY,
        project=p,
        backlog=b,
        kind="ops",
    )


def key_for(user, scope=AgentApiKey.Scope.WRITE):
    token, prefix, secret_hash = mint()
    AgentApiKey.objects.create(
        prefix=prefix, secret_hash=secret_hash, user=user, scope=scope
    )
    return token


def auth(t):
    return {"HTTP_AUTHORIZATION": f"Bearer {t}"}


# --------------------------------------------------------------------------
# the policy helpers
# --------------------------------------------------------------------------


def test_admin_sees_every_task(owner, secret_task):
    assert tasks_visible_to(owner).count() == 1
    assert user_can_see_task(owner, secret_task) is True
    assert visible_project_ids(owner) is None  # None = unrestricted


def test_stranger_sees_nothing(stranger, secret_task):
    assert tasks_visible_to(stranger).count() == 0
    assert user_can_see_task(stranger, secret_task) is False
    assert visible_project_ids(stranger) == []


def test_anonymous_sees_nothing(secret_task):
    from django.contrib.auth.models import AnonymousUser

    assert tasks_visible_to(AnonymousUser()).count() == 0
    assert user_can_see_task(AnonymousUser(), secret_task) is False
    assert visible_project_ids(AnonymousUser()) == []


def test_membership_grants_visibility(owner, stranger, secret_task):
    ProjectMembership.objects.create(
        project=secret_task.project, user=stranger, can_write=False
    )
    assert tasks_visible_to(stranger).count() == 1
    assert user_can_see_task(stranger, secret_task) is True
    assert visible_project_ids(stranger) == [secret_task.project_id]


def test_membership_on_another_project_does_not_help(owner, stranger, secret_task):
    other = Project.objects.create(
        key="other", name="other", created_by=owner, is_default=False
    )
    ProjectMembership.objects.create(project=other, user=stranger, can_write=True)
    assert user_can_see_task(stranger, secret_task) is False


def test_orphan_task_is_invisible_to_everyone_but_admins(owner, secret_task):
    """A task with no project belongs to nothing, so no membership grants it."""
    orphan = Task.objects.create(
        title="ORPHAN", acceptance="x", status=Status.READY, project=None
    )
    member = User.objects.create_user("member", password="pw12345678")
    assert user_can_see_task(member, orphan) is False
    assert user_can_see_task(owner, orphan) is True


# --------------------------------------------------------------------------
# the web UI
# --------------------------------------------------------------------------


def test_board_hides_other_projects_work(client, owner, stranger, secret_task):
    client.force_login(stranger)
    body = client.get("/").content.decode()
    assert "CLASSIFIED PLAN" not in body
    assert "the private goal" not in body


def test_board_shows_own_project_work_after_a_grant(client, owner, stranger, secret_task):
    ProjectMembership.objects.create(
        project=secret_task.project, user=stranger, can_write=True
    )
    client.force_login(stranger)
    body = client.get("/").content.decode()
    assert "CLASSIFIED PLAN" in body


def test_task_detail_is_not_an_idor(client, owner, stranger, secret_task):
    """The defect: any logged-in account could read any card by guessing ids."""
    client.force_login(stranger)
    r = client.get(f"/task/{secret_task.pk}/")
    assert r.status_code == 404
    body = r.content.decode()
    assert "CLASSIFIED PLAN" not in body
    assert "classified acceptance" not in body


def test_task_detail_opens_after_a_grant(client, owner, stranger, secret_task):
    ProjectMembership.objects.create(
        project=secret_task.project, user=stranger, can_write=False
    )
    client.force_login(stranger)
    assert client.get(f"/task/{secret_task.pk}/").status_code == 200


def test_reports_do_not_leak_aggregates(client, owner, stranger, secret_task):
    client.force_login(stranger)
    body = client.get("/reports/").content.decode()
    assert "CLASSIFIED PLAN" not in body


def test_reports_show_the_stuck_list_after_a_grant(client, owner, stranger, secret_task):
    secret_task.stuck_score = 3.0
    secret_task.save()
    ProjectMembership.objects.create(
        project=secret_task.project, user=stranger, can_write=False
    )
    client.force_login(stranger)
    body = client.get("/reports/").content.decode()
    assert "CLASSIFIED PLAN" in body


def test_admin_board_is_unrestricted(client, owner, secret_task):
    client.force_login(owner)
    assert "CLASSIFIED PLAN" in client.get("/").content.decode()


# --------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------


def test_api_list_hides_other_projects(client, owner, stranger, secret_task):
    r = client.get("/api/v1/tasks", **auth(key_for(stranger)))
    assert r.json()["count"] == 0
    assert "CLASSIFIED PLAN" not in r.content.decode()


def test_api_get_by_id_is_not_an_idor(client, owner, stranger, secret_task):
    r = client.get(f"/api/v1/tasks/{secret_task.pk}", **auth(key_for(stranger)))
    assert r.status_code == 404
    assert "CLASSIFIED PLAN" not in r.content.decode()


def test_api_board_state_counts_only_visible(client, owner, stranger, secret_task):
    r = client.get("/api/v1/board", **auth(key_for(stranger)))
    body = r.json()
    assert body["by_status"] == {}
    assert body["total_tokens"] == 0


def test_api_cannot_claim_a_card_from_a_hidden_project(client, owner, stranger, secret_task):
    r = client.post("/api/v1/tasks/claim", {}, **auth(key_for(stranger)))
    assert r.json()["task"] is None
    secret_task.refresh_from_db()
    assert secret_task.status == Status.READY  # untouched


def test_api_cannot_heartbeat_a_card_it_does_not_hold(client, owner, stranger, secret_task):
    r = client.post(
        f"/api/v1/tasks/{secret_task.pk}/heartbeat", {"step": "x"},
        **auth(key_for(stranger)),
    )
    assert r.status_code == 404


def test_api_cannot_submit_review_for_a_hidden_card(client, owner, stranger, secret_task):
    r = client.post(
        f"/api/v1/tasks/{secret_task.pk}/review", {"evidence": {"ok": 1}},
        **auth(key_for(stranger)),
    )
    assert r.status_code == 404
    secret_task.refresh_from_db()
    assert secret_task.status == Status.READY


def test_api_cannot_release_a_hidden_card(client, owner, stranger, secret_task):
    r = client.post(
        f"/api/v1/tasks/{secret_task.pk}/release", {}, **auth(key_for(stranger))
    )
    assert r.status_code == 404


def test_api_cannot_escalate_a_hidden_card(client, owner, stranger, secret_task):
    r = client.post(
        f"/api/v1/tasks/{secret_task.pk}/escalate", {"reason": "x"},
        **auth(key_for(stranger)),
    )
    assert r.status_code == 404
    secret_task.refresh_from_db()
    assert secret_task.needs_human is False


def test_api_granted_member_can_claim_its_project_card(client, owner, stranger, secret_task):
    ProjectMembership.objects.create(
        project=secret_task.project, user=stranger, can_write=True
    )
    r = client.post("/api/v1/tasks/claim", {}, **auth(key_for(stranger)))
    assert r.json()["task"]["title"] == "CLASSIFIED PLAN"


def test_admin_key_claims_anything(client, owner, secret_task):
    r = client.post("/api/v1/tasks/claim", {}, **auth(key_for(owner)))
    assert r.json()["task"]["id"] == secret_task.pk


def test_a_read_only_key_also_cannot_read_a_hidden_card(client, owner, stranger, secret_task):
    r = client.get(
        "/api/v1/tasks", **auth(key_for(stranger, AgentApiKey.Scope.READ))
    )
    assert r.json()["count"] == 0


# --------------------------------------------------------------------------
# filtering by status and kind happens after the scope filter
# --------------------------------------------------------------------------


def test_status_filter_cannot_bypass_the_scope_filter(client, owner, stranger, secret_task):
    r = client.get(
        f"/api/v1/tasks?status={Status.READY}", **auth(key_for(stranger))
    )
    assert r.json()["count"] == 0


def test_kind_filter_cannot_bypass_the_scope_filter(client, owner, stranger, secret_task):
    r = client.get("/api/v1/tasks?kind=ops", **auth(key_for(stranger)))
    assert r.json()["count"] == 0


# --------------------------------------------------------------------------
# the regression that motivated all of this
# --------------------------------------------------------------------------


def test_registration_grants_no_reading_at_all(client, secret_task):
    """
    End to end, through HTTP, from an empty user table.

    Register, be redirected to the board, and receive no card from any project
    — not even a hint of one, and not even through the id in the URL.
    """
    assert User.objects.count() == 1  # the owner who created the task
    body = {
        "username": "brandnew",
        "email": "",
        "password1": "Sup3rSecret!42",
        "password2": "Sup3rSecret!42",
    }
    r = client.post("/register/", body, follow=True)
    assert r.status_code == 200
    page = r.content.decode()
    assert "CLASSIFIED PLAN" not in page
    assert "the private goal" not in page
    assert "No projects yet" in page

    new = User.objects.get(username="brandnew")
    assert new.is_superuser is False
    assert client.get(f"/task/{secret_task.pk}/").status_code == 404

    # and through the API too
    token = key_for(new)
    assert Client().get("/api/v1/tasks", **auth(token)).json()["count"] == 0
