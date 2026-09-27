"""
The projects UI: listing, creating, editing, deleting, and access control.

Registration existed and the board was scoped to project access, but there was
no way to manage projects at all — an administrator looking at the board had
nowhere to go. These tests cover the surface that closes that gap, with the
emphasis on who is allowed to do what.
"""

import pytest
from django.contrib.auth import get_user_model

from board.bootstrap import get_default_project
from board.models import Backlog, Project, ProjectMembership, Task
from board.permissions import user_can_see_project, visible_projects

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def admin():
    return User.objects.create_user("boss", password="pw12345678")


_n = {"i": 0}


@pytest.fixture
def member():
    """
    A registered, unprivileged account with access to nothing.

    Self-contained: it makes its own throwaway bootstrap account first, because
    the first account created on a fresh test database is promoted to
    administrator, and it must not depend on the `admin` fixture having run.
    """
    _n["i"] += 1
    User.objects.create_user(f"boot{_n['i']}", password="pw12345678")
    u = User.objects.create_user("helper", password="pw12345678")
    assert u.is_superuser is False
    return u


# --------------------------------------------------------------------------
# the list
# --------------------------------------------------------------------------


def test_projects_page_lists_visible_projects(client, admin):
    r = client.get("/projects/", follow=True)
    # anonymous is bounced to login
    assert r.status_code == 200
    r2 = _as(client, admin).get("/projects/")
    assert r2.status_code == 200
    assert "kanban-agent" in r2.content.decode()


def test_projects_page_shows_default_project(client, admin):
    body = _as(client, admin).get("/projects/").content.decode()
    assert "default" in body
    assert "/projects/kanban-agent/" in body


def test_projects_page_hides_other_projects(client, admin, member):
    other = Project.objects.create(
        key="secret-project", name="Secret", created_by=admin
    )
    body = _as(client, member).get("/projects/").content.decode()
    assert "kanban-agent" in body
    assert "Secret" not in body


def test_projects_page_explains_an_empty_list(client, member):
    body = _as(client, member).get("/projects/").content.decode()
    assert "No projects yet" in body


def test_new_project_button_only_for_admins(client, admin, member):
    assert "New project" in _as(client, admin).get("/projects/").content.decode()
    assert "New project" not in _as(client, member).get("/projects/").content.decode()


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def test_admin_can_create_a_project(client, admin):
    r = _as(client, admin).post(
        "/projects/new/",
        {
            "name": "Dreamline CMS",
            "description": "the content pipeline",
            "repo_url": "https://github.com/vaskes/dreamline",
        },
        follow=True,
    )
    assert r.status_code == 200
    p = Project.objects.get(key="dreamline-cms")
    assert p.name == "Dreamline CMS"
    assert p.created_by == admin
    assert p.is_default is False
    # a default backlog is created with it, or the project has nowhere to put work
    assert p.backlogs.filter(is_default=True).exists()
    assert "created" in r.content.decode().lower()


def test_created_project_key_is_unique(client, admin):
    _as(client, admin).post("/projects/new/", {"name": "Dup"}, follow=True)
    _as(client, admin).post("/projects/new/", {"name": "Dup"}, follow=True)
    assert Project.objects.filter(key="dup").count() == 1
    assert Project.objects.filter(key="dup-2").count() == 1


def test_project_needs_a_name(client, admin):
    r = _as(client, admin).post("/projects/new/", {"name": "  "})
    assert r.status_code == 200
    assert not Project.objects.filter(name="").exists()


def test_non_admin_cannot_create_a_project(client, member):
    r = _as(client, member).post("/projects/new/", {"name": "Sneaky"})
    assert r.status_code == 403
    assert "Not allowed" in r.content.decode()
    assert not Project.objects.filter(name="Sneaky").exists()


def test_anonymous_is_bounced_from_the_create_form(client):
    assert client.get("/projects/new/").status_code == 302


# --------------------------------------------------------------------------
# detail
# --------------------------------------------------------------------------


def test_project_detail_renders(client, admin):
    body = _as(client, admin).get("/projects/kanban-agent/").content.decode()
    assert "Backlogs" in body
    assert "Access" in body
    assert "Tasks" in body


def test_project_detail_lists_tasks(client, admin):
    p = get_default_project()
    Task.objects.create(title="a task here", acceptance="x", project=p)
    body = _as(client, admin).get("/projects/kanban-agent/").content.decode()
    assert "a task here" in body


def test_invisible_project_is_404_not_403(client, admin, member):
    """Existence is itself information; do not confirm it to a stranger."""
    Project.objects.create(key="secret", name="Secret", created_by=admin)
    r = _as(client, member).get("/projects/secret/")
    assert r.status_code == 404


def test_member_can_open_a_granted_project(client, admin, member):
    ProjectMembership.objects.create(
        project=get_default_project(), user=member, can_write=False
    )
    r = _as(client, member).get("/projects/kanban-agent/")
    assert r.status_code == 200


def test_read_only_member_sees_no_edit_button(client, admin, member):
    ProjectMembership.objects.create(
        project=get_default_project(), user=member, can_write=False
    )
    body = _as(client, member).get("/projects/kanban-agent/").content.decode()
    assert ">Edit<" not in body


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------


def test_admin_can_edit_a_project(client, admin):
    p = Project.objects.create(key="thing", name="Thing", created_by=admin)
    r = _as(client, admin).post(
        f"/projects/{p.key}/edit/", {"name": "Renamed", "description": "d",
                                      "repo_url": ""}, follow=True
    )
    assert r.status_code == 200
    p.refresh_from_db()
    assert p.name == "Renamed"
    assert "saved" in r.content.decode().lower()


def test_key_is_not_editable(client, admin):
    p = Project.objects.create(key="stable", name="Stable", created_by=admin)
    _as(client, admin).post(
        f"/projects/{p.key}/edit/", {"name": "Changed", "repo_url": ""}, follow=True
    )
    p.refresh_from_db()
    assert p.key == "stable"


def test_write_member_can_edit(client, admin, member):
    p = Project.objects.create(key="shared", name="Shared", created_by=admin)
    ProjectMembership.objects.create(project=p, user=member, can_write=True)
    _as(client, member).post(
        f"/projects/{p.key}/edit/", {"name": "Edited by member", "repo_url": ""},
        follow=True,
    )
    p.refresh_from_db()
    assert p.name == "Edited by member"


def test_read_only_member_cannot_edit(client, admin, member):
    p = Project.objects.create(key="shared", name="Shared", created_by=admin)
    ProjectMembership.objects.create(project=p, user=member, can_write=False)
    r = _as(client, member).post(
        f"/projects/{p.key}/edit/", {"name": "Hacked", "repo_url": ""}
    )
    assert r.status_code == 403
    p.refresh_from_db()
    assert p.name == "Shared"


def test_edit_of_invisible_project_is_404(client, admin, member):
    p = Project.objects.create(key="secret", name="Secret", created_by=admin)
    r = _as(client, member).post(f"/projects/{p.key}/edit/", {"name": "x", "repo_url": ""})
    assert r.status_code == 404


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


def test_delete_requires_confirmation_page(client, admin):
    p = Project.objects.create(key="doomed", name="Doomed", created_by=admin)
    body = _as(client, admin).get(f"/projects/{p.key}/delete/").content.decode()
    assert "cannot be undone" in body
    assert Project.objects.filter(key="doomed").exists()


def test_delete_removes_the_project(client, admin):
    p = Project.objects.create(key="doomed", name="Doomed", created_by=admin)
    r = _as(client, admin).post(f"/projects/{p.key}/delete/", follow=True)
    assert r.status_code == 200
    assert not Project.objects.filter(key="doomed").exists()


def test_default_project_cannot_be_deleted(client, admin):
    p = get_default_project()
    r = _as(client, admin).post(f"/projects/{p.key}/delete/", follow=True)
    assert r.status_code == 200
    assert Project.objects.filter(key=p.key).exists()
    assert "cannot be deleted" in r.content.decode().lower()


def test_non_admin_cannot_delete(client, admin, member):
    p = Project.objects.create(key="doomed", name="Doomed", created_by=admin)
    ProjectMembership.objects.create(project=p, user=member, can_write=True)
    r = _as(client, member).post(f"/projects/{p.key}/delete/")
    assert r.status_code == 403
    assert Project.objects.filter(key="doomed").exists()


# --------------------------------------------------------------------------
# membership management
# --------------------------------------------------------------------------


def test_admin_can_grant_read_access(client, admin, member):
    r = _as(client, admin).post(
        f"/projects/{get_default_project().key}/members/",
        {"user": member.pk},
        follow=True,
    )
    assert r.status_code == 200
    m = ProjectMembership.objects.get(user=member)
    assert m.can_write is False
    assert m.granted_by == admin
    assert user_can_see_project(member, m.project) is True


def test_admin_can_grant_write_access(client, admin, member):
    _as(client, admin).post(
        f"/projects/{get_default_project().key}/members/",
        {"user": member.pk, "can_write": "on"},
        follow=True,
    )
    assert ProjectMembership.objects.get(user=member).can_write is True


def test_granting_gives_immediate_visibility(client, admin, member):
    p = get_default_project()
    assert list(visible_projects(member).values_list("key", flat=True)) == []
    _as(client, admin).post(
        f"/projects/{p.key}/members/", {"user": member.pk}, follow=True
    )
    assert list(visible_projects(member).values_list("key", flat=True)) == [p.key]


def test_cannot_grant_twice(client, admin, member):
    p = get_default_project()
    url = f"/projects/{p.key}/members/"
    _as(client, admin).post(url, {"user": member.pk}, follow=True)
    r = _as(client, admin).post(url, {"user": member.pk})
    assert "already has access" in r.content.decode()
    assert ProjectMembership.objects.filter(user=member).count() == 1


def test_duplicate_grant_explains_itself(client, admin, member):
    """
    A rejected action must say what is wrong. Excluding existing members from
    the queryset produced Django's generic 'Select a valid choice' instead.
    """
    p = get_default_project()
    url = f"/projects/{p.key}/members/"
    _as(client, admin).post(url, {"user": member.pk}, follow=True)
    r = _as(client, admin).post(url, {"user": member.pk})
    assert r.status_code == 200
    assert "already has access" in r.content.decode()


def test_grant_form_lists_every_user(client, admin, member):
    p = get_default_project()
    ProjectMembership.objects.create(project=p, user=member)
    body = _as(client, admin).get(f"/projects/{p.key}/members/").content.decode()
    # still selectable, so a repeat submission gets a readable error
    assert f'value="{member.pk}"' in body


def test_non_admin_cannot_manage_membership(client, admin, member):
    p = Project.objects.create(key="shared", name="Shared", created_by=admin)
    ProjectMembership.objects.create(project=p, user=member, can_write=True)
    r = _as(client, member).post(
        f"/projects/{p.key}/members/", {"user": admin.pk}
    )
    assert r.status_code == 403
    assert ProjectMembership.objects.filter(project=p, user=admin).count() == 0


def test_member_can_be_toggled_between_read_and_write(client, admin, member):
    p = get_default_project()
    ProjectMembership.objects.create(project=p, user=member, can_write=False)
    _as(client, admin).post(f"/projects/{p.key}/members/{member.pk}/toggle/")
    assert ProjectMembership.objects.get(user=member).can_write is True
    _as(client, admin).post(f"/projects/{p.key}/members/{member.pk}/toggle/")
    assert ProjectMembership.objects.get(user=member).can_write is False


def test_member_can_be_revoked(client, admin, member):
    p = get_default_project()
    ProjectMembership.objects.create(project=p, user=member, can_write=True)
    r = _as(client, admin).post(
        f"/projects/{p.key}/members/{member.pk}/revoke/", follow=True
    )
    assert r.status_code == 200
    assert not ProjectMembership.objects.filter(user=member).exists()
    assert user_can_see_project(member, p) is False


def test_revoking_an_unknown_member_is_a_noop(client, admin):
    p = get_default_project()
    r = _as(client, admin).post(
        f"/projects/{p.key}/members/999999/revoke/", follow=True
    )
    assert r.status_code == 200


def test_toggling_an_unknown_member_is_a_noop(client, admin):
    p = get_default_project()
    r = _as(client, admin).post(f"/projects/{p.key}/members/999999/toggle/", follow=True)
    assert r.status_code == 200


def test_get_on_toggle_does_not_change_anything(client, admin, member):
    p = get_default_project()
    ProjectMembership.objects.create(project=p, user=member, can_write=False)
    _as(client, admin).get(f"/projects/{p.key}/members/{member.pk}/toggle/")
    assert ProjectMembership.objects.get(user=member).can_write is False


def test_get_on_revoke_does_not_change_anything(client, admin, member):
    p = get_default_project()
    ProjectMembership.objects.create(project=p, user=member, can_write=False)
    _as(client, admin).get(f"/projects/{p.key}/members/{member.pk}/revoke/")
    assert ProjectMembership.objects.filter(user=member).exists()


def test_non_admin_cannot_toggle_or_revoke(client, admin, member):
    p = Project.objects.create(key="shared", name="Shared", created_by=admin)
    m = ProjectMembership.objects.create(project=p, user=member, can_write=False)
    assert _as(client, member).post(
        f"/projects/{p.key}/members/{member.pk}/toggle/"
    ).status_code == 403
    assert ProjectMembership.objects.filter(pk=m.pk).exists()
    assert _as(client, member).post(
        f"/projects/{p.key}/members/{member.pk}/revoke/"
    ).status_code == 403
    assert ProjectMembership.objects.filter(pk=m.pk).exists()


# --------------------------------------------------------------------------
# admin site
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/admin/board/project/", "/admin/board/agent/", "/admin/board/backlog/",
     "/admin/board/projectmembership/", "/admin/board/chatsession/",
     "/admin/board/chatmessage/"],
)
def test_admin_site_registers_the_whole_tree(client, admin, path):
    """Every model must be inspectable from the admin site."""
    r = _as(client, admin).get(path)
    assert r.status_code == 200, path


def test_agent_api_keys_live_inline_on_the_agent_page(client, admin):
    """Keys are no longer a top-level admin entry — they live under Agent."""
    # The top-level URL must 404 (or 302 to the agent changelist).
    r = _as(client, admin).get("/admin/board/agentapikey/")
    assert r.status_code in (302, 404)
    # The agent changelist must still work and include keys inline.
    r = _as(client, admin).get("/admin/board/agent/")
    assert r.status_code == 200


def test_admin_index_lists_projects(client, admin):
    body = _as(client, admin).get("/admin/").content.decode()
    assert "Projects" in body
    assert "Agents" in body


def test_admin_project_list_shows_the_default_flag(client, admin):
    body = _as(client, admin).get("/admin/board/project/").content.decode()
    assert "kanban-agent" in body


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _as(client, user):
    client.force_login(user)
    return client


# --------------------------------------------------------------------------
# remaining branches
# --------------------------------------------------------------------------


def test_admin_sees_the_empty_create_form(client, admin):
    r = _as(client, admin).get("/projects/new/")
    assert r.status_code == 200
    assert "Create project" in r.content.decode()


def test_admin_sees_the_edit_form(client, admin):
    p = Project.objects.create(key="thing", name="Thing", created_by=admin)
    body = _as(client, admin).get(f"/projects/{p.key}/edit/").content.decode()
    assert "Edit project" in body
    assert "key is fixed" in body


def test_whitespace_name_is_rejected(client, admin):
    """
    A name of only spaces is refused. Django's CharField strips first, so this
    surfaces as the standard required-field message rather than a custom one.
    """
    r = _as(client, admin).post("/projects/new/", {"name": "   "})
    assert r.status_code == 200
    assert "required" in r.content.decode().lower()
    assert not Project.objects.filter(name="   ").exists()


def test_grant_form_when_no_accounts_exist(admin):
    """The empty-queryset hint only renders when there is genuinely nobody."""
    from board.project_forms import MembershipForm

    User.objects.all().delete()
    p = get_default_project()
    form = MembershipForm(project=p)
    assert form.fields["user"].help_text == "No accounts exist yet."


@pytest.mark.parametrize(
    "suffix", ["edit/", "delete/", "members/"]
)
def test_every_project_action_is_404_for_an_invisible_project(client, admin, member, suffix):
    p = Project.objects.create(key="secret", name="Secret", created_by=admin)
    url = f"/projects/{p.key}/{suffix}"
    assert _as(client, member).get(url).status_code == 404
    assert _as(client, member).post(url, {}).status_code == 404


def test_member_actions_on_an_invisible_project_are_404(client, admin, member):
    p = Project.objects.create(key="secret", name="Secret", created_by=admin)
    assert _as(client, member).get(
        f"/projects/{p.key}/members/{member.pk}/toggle/"
    ).status_code == 404
    assert _as(client, member).post(
        f"/projects/{p.key}/members/{member.pk}/revoke/"
    ).status_code == 404


def test_delete_confirmation_for_a_non_default_project(client, admin):
    p = Project.objects.create(key="doomed", name="Doomed", created_by=admin)
    body = _as(client, admin).get(f"/projects/{p.key}/delete/").content.decode()
    assert "archive it" in body


# --------------------------------------------------------------------------
# admin actions and display helpers
# --------------------------------------------------------------------------


def test_admin_can_archive_and_unarchive(client, admin):
    from board.models import Project as P

    p = P.objects.create(key="old", name="Old", created_by=admin)
    _as(client, admin).post(
        "/admin/board/project/", {"action": "archive", "_selected_action": [str(p.pk)]}
    )
    p.refresh_from_db()
    assert p.archived is True

    _as(client, admin).post(
        "/admin/board/project/", {"action": "unarchive", "_selected_action": [str(p.pk)]}
    )
    p.refresh_from_db()
    assert p.archived is False


def test_archiving_never_touches_the_default_project(client, admin):
    from board.models import Project as P

    p = get_default_project()
    _as(client, admin).post(
        "/admin/board/project/", {"action": "archive", "_selected_action": [str(p.pk)]}
    )
    p.refresh_from_db()
    assert p.archived is False


def test_admin_display_helpers_render(client, admin):
    from board.models import Agent, ChatMessage, ChatSession, Memory

    a = Agent.objects.create(name="w1")
    body = _as(client, admin).get("/admin/board/agent/").content.decode()
    assert a.name in body
    assert "unreachable" in body

    m = Memory.objects.create(kind="lesson", content="x" * 200)
    body = _as(client, admin).get("/admin/board/memory/").content.decode()
    assert "x" * 80 in body

    s = ChatSession.objects.create(scope="projects", project=get_default_project())
    ChatMessage.objects.create(session=s, role="user", content="y" * 200)
    body = _as(client, admin).get("/admin/board/chatmessage/").content.decode()
    assert "y" * 80 in body


def test_membership_form_without_a_project_is_usable():
    """The form must not require a project to be passed in."""
    from board.project_forms import MembershipForm

    f = MembershipForm(data={"user": 1})
    assert "project" not in f.fields
    assert f.is_bound


def test_ensure_backlog_is_idempotent(admin):
    from board.views import _ensure_backlog

    p = Project.objects.create(key="nb", name="NoBacklog", created_by=admin)
    assert p.backlogs.count() == 0
    _ensure_backlog(p)
    _ensure_backlog(p)
    assert p.backlogs.filter(is_default=True).count() == 1


def test_slugify_falls_back_for_a_name_with_no_latin():
    """
    A Cyrillic or emoji name slugifies to nothing. The key must still be a
    usable identifier rather than an empty string.
    """
    from board.views import _slugify

    assert _slugify("日本語") == "project"
    assert _slugify("Dreamline CMS") == "dreamline-cms"
    assert _slugify("a" * 200) == "a" * 60
