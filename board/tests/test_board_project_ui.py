"""
The board is per-project. Tests for the picker, the header and the empty
state live here, so the user-facing behaviour of '/' is covered even when the
permission and state-machine tests are rewritten.
"""

import pytest

from board.bootstrap import ensure_default_project
from board.models import (
    Backlog,
    Project,
    ProjectMembership,
    Status,
    Task,
)


pytestmark = pytest.mark.django_db


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def board_url():
    return "/"


@pytest.fixture
def default_project():
    return ensure_default_project()


@pytest.fixture(autouse=True)
def _bootstrap_first_user(django_user_model):
    """
    The very first user created on a fresh DB becomes admin by signal.
    Every test here wants a NON-admin operator, so seed a throwaway admin
    first and let the real fixtures create their own user afterwards.
    """
    django_user_model.objects.create_user("boot", password="pw12345")


def _make_user(django_user_model, name, **extra):
    return django_user_model.objects.create_user(name, password="pw12345",
                                                 **extra)


def _client_for(user):
    from django.test import Client
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def operator(django_user_model, default_project):
    """Non-admin who is a read+write member of the default project."""
    u = _make_user(django_user_model, "op")
    assert u.is_superuser is False
    ProjectMembership.objects.create(
        project=default_project, user=u, can_write=True,
    )
    return u


@pytest.fixture
def second_project(operator):
    p = Project.objects.create(
        key="second", name="Second project", created_by=operator,
        description="another team's home",
        repo_url="https://github.com/vaskes/example",
    )
    Backlog.objects.create(project=p, name="Backlog", is_default=True)
    return p


@pytest.fixture
def admin_user(django_user_model):
    return _make_user(django_user_model, "root",
                      is_staff=True, is_superuser=True)


# --- basics ---------------------------------------------------------------


def test_board_picks_default_project_when_no_query(board_url, operator,
                                                   default_project):
    r = _client_for(operator).get(board_url)
    assert r.status_code == 200
    body = r.content.decode()
    assert default_project.name in body
    assert default_project.key in body


def test_board_renders_project_header_with_name_key_repo_and_member_count(
    board_url, operator, second_project,
):
    r = _client_for(operator).get(f"{board_url}?project=second")
    assert r.status_code == 200
    body = r.content.decode()
    assert "Second project" in body
    assert ">second<" in body
    assert "github.com/vaskes/example" in body
    assert "another team" in body  # description


def test_board_renders_column_labels(board_url, operator, default_project):
    Task.objects.create(title="t", acceptance="x", status=Status.READY,
                        project=default_project)
    r = _client_for(operator).get(board_url)
    body = r.content.decode()
    for label in ("Inbox", "Backlog", "Ready", "In progress", "Review", "Done"):
        assert label in body, f"missing column {label}"


def test_board_renders_an_empty_state_for_a_project_with_no_tasks(
    board_url, operator, second_project,
):
    r = _client_for(operator).get(f"{board_url}?project=second")
    body = r.content.decode()
    assert "no tasks yet" in body.lower()
    # and the column skeleton is still there so the page never looks broken
    assert 'class="cols"' in body


def test_board_filters_tasks_to_the_current_project(
    board_url, operator, second_project, default_project,
):
    Task.objects.create(title="in-default", acceptance="x",
                        status=Status.READY, project=default_project)
    Task.objects.create(title="in-second", acceptance="x",
                        status=Status.READY, project=second_project)
    r = _client_for(operator).get(f"{board_url}?project=second")
    body = r.content.decode()
    assert "in-second" in body
    assert "in-default" not in body


# --- picker ---------------------------------------------------------------


def test_tabs_appear_when_two_or_more_projects_are_visible(
    board_url, operator, second_project, default_project,
):
    r = _client_for(operator).get(board_url)
    body = r.content.decode()
    assert default_project.name in body
    assert "Second project" in body
    assert 'aria-current="page"' in body


def test_tabs_do_not_render_for_a_single_project(board_url, operator):
    r = _client_for(operator).get(board_url)
    body = r.content.decode()
    assert 'class="tabs"' not in body


def test_tab_count_reflects_each_projects_task_count(
    board_url, operator, second_project, default_project,
):
    Task.objects.create(title="t1", acceptance="x", status=Status.READY,
                        project=default_project)
    Task.objects.create(title="t2", acceptance="x", status=Status.BACKLOG,
                        project=default_project)
    Task.objects.create(title="t3", acceptance="x", status=Status.READY,
                        project=second_project)
    r = _client_for(operator).get(f"{board_url}?project=second")
    body = r.content.decode()
    nav_start = body.find('class="tabs"')
    nav_end = body.find('</nav>', nav_start)
    nav = body[nav_start:nav_end]
    # both counters must be present, one per tab
    assert nav.count('class="n"') == 2


def test_unknown_project_falls_back_silently_to_default(
    board_url, operator, second_project, default_project,
):
    r = _client_for(operator).get(f"{board_url}?project=does-not-exist")
    assert r.status_code == 200
    body = r.content.decode()
    assert default_project.name in body
    assert "does-not-exist" not in body


def test_tabs_do_not_leak_invisible_projects(
    board_url, operator, second_project,
):
    other = Project.objects.create(key="other", name="Other")
    Backlog.objects.create(project=other, name="Backlog", is_default=True)
    r = _client_for(operator).get(board_url)
    body = r.content.decode()
    assert ">Other<" not in body
    assert '>other<' not in body


# --- admin chrome ---------------------------------------------------------


def test_settings_link_appears_only_for_admins(
    board_url, operator, default_project, admin_user,
):
    r = _client_for(operator).get(board_url)
    body = r.content.decode()
    assert "/edit/" not in body

    r2 = _client_for(admin_user).get(board_url)
    assert "/edit/" in r2.content.decode()


def test_settings_link_points_at_the_current_project(
    board_url, second_project, admin_user,
):
    r = _client_for(admin_user).get(f"{board_url}?project=second")
    body = r.content.decode()
    assert "/projects/second/edit/" in body


# --- no-projects empty state ---------------------------------------------


def test_user_with_no_projects_sees_a_clear_empty_state(
    board_url, django_user_model,
):
    u = _make_user(django_user_model, "outsider")
    r = _client_for(u).get(board_url)
    assert r.status_code == 200
    body = r.content.decode()
    assert "No projects yet" in body
    assert 'class="cols"' not in body
    assert 'class="phead"' not in body
    assert 'class="tabs"' not in body


# --- default project marker ----------------------------------------------


def test_default_project_tab_is_marked_with_a_star(
    board_url, operator, second_project, default_project,
):
    r = _client_for(operator).get(board_url)
    body = r.content.decode()
    # The default tab carries the `def` marker class, regardless of whether
    # it is also the active one.
    assert ' def' in body
    # And the active tab is present too — the default project is what an
    # operator with no ?project= param lands on.
    assert 'aria-current="page"' in body


# --- task counter is scope-aware -----------------------------------------


def test_task_counter_uses_the_same_scoping_as_the_columns(
    board_url, operator, second_project, django_user_model,
):
    """
    A card in a project the operator is NOT a member of must not bump the
    counter on the operator's own tabs.
    """
    foreign = Project.objects.create(key="foreign", name="Foreign")
    Backlog.objects.create(project=foreign, name="Backlog", is_default=True)
    # the operator cannot see foreign's tasks, but a task exists
    Task.objects.create(title="hidden-from-you", acceptance="x",
                        status=Status.READY, project=foreign)
    # their own membership's project has zero
    r = _client_for(operator).get(board_url)
    body = r.content.decode()
    assert "hidden-from-you" not in body
    # the tab bar must not show a foreign tab either
    assert ">Foreign<" not in body
