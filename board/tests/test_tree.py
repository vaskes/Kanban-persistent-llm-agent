"""
Tests for the project / backlog / task tree, the scope policy, agent
registration, and the default project.

The policy tests are the point of this file: they encode who may change what,
and an error there is not a crash, it is an agent silently doing something it
was never allowed to do.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from io import StringIO

from board.bootstrap import (
    ensure_default_project,
    get_default_backlog,
    get_default_project,
)
from board.models import (
    Agent,
    Backlog,
    ChatMessage,
    ChatScope,
    ChatSession,
    DEFAULT_PROJECT_KEY,
    Project,
    ProjectMembership,
    ProtectedDefaultProject,
    Status,
    Task,
)
from board.permissions import (
    PermissionError_,
    can_assign,
    grant_for,
    user_can_see_project,
    user_can_write_project,
    user_may_create_projects,
    visible_projects,
)

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def admin():
    return User.objects.create_user("boss", password="pw", is_staff=True, is_superuser=True)


@pytest.fixture
def plain():
    return User.objects.create_user("op", password="pw")


@pytest.fixture
def nonadmin():
    """
    A user that is definitely not an administrator.

    The bootstrap rule promotes whichever account is created first on a fresh
    database, and the test database is rebuilt per test. So a test that needs an
    unprivileged user must not use whichever fixture happens to run first.
    """
    User.objects.create_user("bootstrap", password="pw")  # becomes the admin
    u = User.objects.create_user("regular", password="pw")
    assert u.is_superuser is False
    return u


def make_project(key, user, **kw):
    return Project.objects.create(
        key=key, name=key, created_by=user, is_default=False, **kw
    )


def make_backlog(project, name="Backlog"):
    return Backlog.objects.create(project=project, name=name, is_default=True)


# --------------------------------------------------------------------------
# default project
# --------------------------------------------------------------------------


def test_default_project_exists_after_migration(admin):
    p = Project.objects.get(key=DEFAULT_PROJECT_KEY)
    assert p.is_default is True
    assert p.name == "kanban-agent"
    assert p.repo_url == "https://github.com/vaskes/Kanban-persistent-llm-agent"


def test_default_project_has_a_default_backlog():
    p = get_default_project()
    assert p.backlogs.filter(is_default=True).count() == 1


def test_ensure_default_project_is_idempotent():
    a = ensure_default_project()
    b = ensure_default_project()
    assert a.pk == b.pk
    assert Project.objects.filter(key=DEFAULT_PROJECT_KEY).count() == 1
    assert a.backlogs.filter(is_default=True).count() == 1


def test_ensure_default_project_repairs_a_lost_flag():
    p = Project.objects.get(key=DEFAULT_PROJECT_KEY)
    p.is_default = False
    p.save()
    ensure_default_project()
    p.refresh_from_db()
    assert p.is_default is True


def test_ensure_default_project_recreates_a_deleted_project():
    Project.objects.filter(key=DEFAULT_PROJECT_KEY).delete()
    p = ensure_default_project()
    assert p.is_default is True
    assert p.backlogs.filter(is_default=True).exists()


def test_get_default_backlog_creates_if_missing():
    p = get_default_project()
    p.backlogs.all().delete()
    b = get_default_backlog(p)
    assert b.is_default is True


def test_get_default_project_heals():
    Project.objects.filter(key=DEFAULT_PROJECT_KEY).delete()
    assert get_default_project().is_default is True


def test_ensure_command_runs_clean():
    out = StringIO()
    call_command("ensure_default_project", stdout=out)
    text = out.getvalue()
    assert DEFAULT_PROJECT_KEY in text
    assert "Backlog" in text


def test_system_check_passes_when_healthy():
    from board.bootstrap import default_project_exists

    assert default_project_exists(None) == []


def test_system_check_flags_a_missing_default():
    from board.bootstrap import default_project_exists

    Project.objects.all().delete()
    problems = default_project_exists(None)
    assert any(getattr(p, "id", "") == "board.E001" for p in problems)


def test_system_check_flags_two_defaults():
    """
    The duplicate-default state is only reachable by hand-editing the table —
    Project.save() pins the key of any default project, so a second one cannot
    be created through the ORM. That is exactly why the check exists.
    """
    from django.db import connection

    from board.bootstrap import default_project_exists

    u = User.objects.create_user("x", password="pw")
    p = make_project("other", u)
    # bypass the pinning deliberately, as a careless UPDATE would
    with connection.cursor() as cur:
        cur.execute("UPDATE projects SET is_default = true WHERE key = 'other'")

    problems = default_project_exists(None)
    assert any(getattr(pr, "id", "") == "board.E002" for pr in problems)


def test_system_check_warns_about_missing_backlog():
    from board.bootstrap import default_project_exists

    Project.objects.get(key=DEFAULT_PROJECT_KEY).backlogs.all().delete()
    problems = default_project_exists(None)
    assert any(getattr(p, "id", "") == "board.W001" for p in problems)


# --------------------------------------------------------------------------
# default project cannot be removed or renamed
# --------------------------------------------------------------------------


def test_default_project_cannot_be_deleted():
    p = get_default_project()
    with pytest.raises(ProtectedDefaultProject):
        p.delete()


def test_default_project_key_is_pinned_on_save():
    p = get_default_project()
    p.key = "renamed"
    p.name = "renamed"
    p.save()
    p.refresh_from_db()
    assert p.key == DEFAULT_PROJECT_KEY
    assert p.name == "kanban-agent"


def test_non_default_project_can_be_deleted(admin):
    p = make_project("scratch", admin)
    p.delete()
    assert not Project.objects.filter(key="scratch").exists()


def test_only_one_default_backlog_per_project(admin):
    p = make_project("p1", admin)
    make_backlog(p)
    with pytest.raises(Exception):
        make_backlog(p)


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------


def test_agent_starts_unreachable():
    a = Agent.objects.create(name="w1")
    assert a.status == Agent.Status.UNREACHABLE
    assert a.is_reachable is False


def test_agent_is_reachable_after_heartbeat():
    a = Agent.objects.create(name="w1")
    a.heartbeat()
    a.refresh_from_db()
    assert a.status == Agent.Status.REACHABLE


def test_agent_goes_unreachable_after_the_window():
    from django.utils import timezone

    a = Agent.objects.create(name="w1")
    Agent.objects.filter(pk=a.pk).update(
        last_seen_at=timezone.now()
        - timezone.timedelta(seconds=Agent.REACHABLE_WINDOW_SECONDS + 10)
    )
    a.refresh_from_db()
    assert a.status == Agent.Status.UNREACHABLE


def test_agent_names_are_unique():
    Agent.objects.create(name="w1")
    with pytest.raises(Exception):
        Agent.objects.create(name="w1")


def test_agent_str_includes_status():
    a = Agent.objects.create(name="w1")
    assert "unreachable" in str(a)


# --------------------------------------------------------------------------
# task placement and assignment
# --------------------------------------------------------------------------


def test_task_defaults_to_any_assignee():
    t = Task.objects.create(title="t", acceptance="x", status=Status.READY)
    assert t.assignee is None
    assert t.project is None
    assert t.backlog is None


def test_task_can_be_assigned_to_a_registered_agent():
    a = Agent.objects.create(name="w1")
    t = Task.objects.create(
        title="t", acceptance="x", status=Status.READY, assignee=a
    )
    assert t.assignee == a
    assert list(a.assigned_tasks.all()) == [t]


def test_task_lives_in_a_backlog_of_a_project(admin):
    p = get_default_project()
    b = get_default_backlog(p)
    t = Task.objects.create(
        title="t", acceptance="x", status=Status.READY, project=p, backlog=b
    )
    assert t.project == p
    assert t.backlog == b


def test_any_assignee_is_always_allowed():
    assert can_assign(None) is True
    assert can_assign(Agent.objects.create(name="w")) is True


def test_unregistered_agent_cannot_be_assigned():
    """
    An unsaved Agent instance has a primary key but no row. It must not be
    assignable, or a stale form could reference an agent that does not exist.
    """
    stray = Agent(id="not-registered", name="ghost")
    assert can_assign(stray) is False


# --------------------------------------------------------------------------
# project membership
# --------------------------------------------------------------------------


def test_creator_can_see_and_write_their_project(admin, plain):
    p = make_project("mine", admin)
    assert user_can_see_project(plain, p) is False
    m = ProjectMembership.objects.create(project=p, user=plain, can_write=True)
    assert user_can_see_project(plain, p) is True
    assert user_can_write_project(plain, p) is True


def test_read_membership_grants_read_not_write(admin, plain):
    p = make_project("shared", admin)
    ProjectMembership.objects.create(project=p, user=plain, can_write=False)
    assert user_can_see_project(plain, p) is True
    assert user_can_write_project(plain, p) is False


def test_admin_sees_everything(admin, plain):
    p = make_project("secret", plain)
    assert user_can_see_project(admin, p) is True
    assert user_can_write_project(admin, p) is True


def test_anonymous_sees_nothing(admin):
    from django.contrib.auth.models import AnonymousUser

    p = make_project("p", admin)
    assert user_can_see_project(AnonymousUser(), p) is False
    assert user_may_create_projects(AnonymousUser()) is False


def test_only_admins_may_create_projects(admin, plain):
    assert user_may_create_projects(admin) is True
    assert user_may_create_projects(plain) is False


def test_creator_has_write_without_a_membership_row(admin):
    p = make_project("mine", admin)
    assert not p.memberships.exists()
    assert user_can_write_project(admin, p) is True


def test_visible_projects_respects_memberships(admin, plain):
    a = make_project("a", admin)
    b = make_project("b", admin)
    ProjectMembership.objects.create(project=b, user=plain)
    names = set(visible_projects(plain).values_list("key", flat=True))
    assert names == {"b"}


def test_visible_projects_includes_created(admin, plain):
    a = make_project("own", plain)
    assert "own" in set(visible_projects(plain).values_list("key", flat=True))


def test_visible_projects_is_empty_for_anonymous(admin):
    from django.contrib.auth.models import AnonymousUser

    make_project("a", admin)
    assert visible_projects(AnonymousUser()).count() == 0


def test_membership_is_unique_per_user(admin, plain):
    p = make_project("p", admin)
    ProjectMembership.objects.create(project=p, user=plain)
    with pytest.raises(Exception):
        ProjectMembership.objects.create(project=p, user=plain)


def test_membership_str(admin, plain):
    p = make_project("p", admin)
    m = ProjectMembership.objects.create(project=p, user=plain, can_write=True)
    assert "rw" in str(m)


def test_grant_by_membership_without_write(admin, plain):
    p = make_project("p", admin)
    m = ProjectMembership.objects.create(project=p, user=plain, can_write=False)
    assert "ro" in str(m)


# --------------------------------------------------------------------------
# scope policy
# --------------------------------------------------------------------------


def test_projects_scope_changes_projects():
    g = grant_for(ChatScope.PROJECTS)
    g.assert_can_change("project")
    assert g.can_delete_projects is True


def test_projects_scope_cannot_change_tasks_or_backlogs():
    g = grant_for(ChatScope.PROJECTS)
    with pytest.raises(PermissionError_):
        g.assert_can_change("task")
    with pytest.raises(PermissionError_):
        g.assert_can_change("backlog")


def test_backlog_scope_changes_tasks():
    g = grant_for(ChatScope.BACKLOG)
    g.assert_can_change("task", target_task_id="t1", anchor_task_id="t1")
    assert g.can_change_projects is False


def test_backlog_scope_cannot_change_projects():
    g = grant_for(ChatScope.BACKLOG)
    with pytest.raises(PermissionError_):
        g.assert_can_change("project")


def test_task_scope_changes_only_its_own_task():
    g = grant_for(ChatScope.TASK)
    g.assert_can_change("task", target_task_id="t1", anchor_task_id="t1")
    with pytest.raises(PermissionError_, match="anchored to"):
        g.assert_can_change("task", target_task_id="t2", anchor_task_id="t1")


def test_task_scope_cannot_change_projects_or_backlogs():
    g = grant_for(ChatScope.TASK)
    with pytest.raises(PermissionError_):
        g.assert_can_change("project")
    with pytest.raises(PermissionError_):
        g.assert_can_change("backlog")


def test_every_scope_can_read_the_whole_tree():
    """Reading is not the same as writing: all three read outward."""
    for scope in (ChatScope.PROJECTS, ChatScope.BACKLOG, ChatScope.TASK):
        g = grant_for(scope)
        g.assert_can_read("project")
        g.assert_can_read("backlog")
        g.assert_can_read("task")


def test_unknown_resource_kind_is_refused():
    g = grant_for(ChatScope.TASK)
    with pytest.raises(PermissionError_, match="unknown resource"):
        g.assert_can_change("widget")
    with pytest.raises(PermissionError_, match="unknown resource"):
        g.assert_can_read("widget")


def test_unknown_scope_raises():
    with pytest.raises(PermissionError_, match="unknown chat scope"):
        grant_for("galaxy")


def test_grant_labels_describe_the_level():
    assert grant_for(ChatScope.PROJECTS).can_change == "projects"
    assert grant_for(ChatScope.BACKLOG).can_change == "tasks in this backlog"
    assert grant_for(ChatScope.TASK).can_change == "this task"


# --------------------------------------------------------------------------
# chat sessions
# --------------------------------------------------------------------------


def test_project_scoped_session_needs_no_backlog_or_task():
    p = get_default_project()
    s = ChatSession.objects.create(
        scope=ChatScope.PROJECTS, project=p, created_by=p.created_by
    )
    assert s.anchor_label().endswith("(projects)")


def test_backlog_scoped_session_anchors_to_a_backlog():
    p = get_default_project()
    b = get_default_backlog(p)
    s = ChatSession.objects.create(
        scope=ChatScope.BACKLOG, project=p, backlog=b, created_by=p.created_by
    )
    assert DEFAULT_PROJECT_KEY in s.anchor_label()


def test_task_scoped_session_anchors_to_a_task():
    p = get_default_project()
    b = get_default_backlog(p)
    t = Task.objects.create(
        title="a specific task", acceptance="x", status=Status.READY, project=p, backlog=b
    )
    s = ChatSession.objects.create(
        scope=ChatScope.TASK, project=p, backlog=b, task=t, created_by=p.created_by
    )
    assert "a specific task" in s.anchor_label()


def test_task_scope_without_a_task_is_refused():
    p = get_default_project()
    with pytest.raises(Exception):
        ChatSession.objects.create(
            scope=ChatScope.TASK, project=p, backlog=get_default_backlog(p)
        )


def test_projects_scope_with_a_backlog_is_refused():
    p = get_default_project()
    with pytest.raises(Exception):
        ChatSession.objects.create(
            scope=ChatScope.PROJECTS, project=p, backlog=get_default_backlog(p)
        )


def test_chat_messages_order_chronologically():
    p = get_default_project()
    s = ChatSession.objects.create(
        scope=ChatScope.PROJECTS, project=p, created_by=p.created_by
    )
    ChatMessage.objects.create(session=s, role="user", content="first")
    ChatMessage.objects.create(session=s, role="agent", content="second")
    assert [m.content for m in s.messages.all()] == ["first", "second"]


def test_chat_message_str():
    p = get_default_project()
    s = ChatSession.objects.create(
        scope=ChatScope.PROJECTS, project=p, created_by=p.created_by
    )
    m = ChatMessage.objects.create(session=s, role="user", content="hello there")
    assert "user: hello there" == str(m)


def test_session_deletes_with_its_project(admin):
    p = make_project("throwaway", admin)
    s = ChatSession.objects.create(
        scope=ChatScope.PROJECTS, project=p, created_by=admin
    )
    ChatMessage.objects.create(session=s, role="user", content="x")
    old = s.pk
    p.delete()
    assert not ChatSession.objects.filter(pk=old).exists()


# --------------------------------------------------------------------------
# coverage for the remaining policy and bootstrap branches
# --------------------------------------------------------------------------


def test_read_permissibility_of_an_unsettable_kind():
    """A grant with reads switched off must actually refuse."""
    from board.permissions import Grant

    g = Grant(scope=ChatScope.TASK, can_read_backlogs=False)
    with pytest.raises(PermissionError_, match="may not read backlogs"):
        g.assert_can_read("backlog")
    g.assert_can_read("task")


def test_backlog_change_refusal_names_the_level():
    g = grant_for(ChatScope.BACKLOG)
    with pytest.raises(PermissionError_, match="backlogs"):
        g.assert_can_change("backlog")


def test_null_owner_still_allows_admin_and_membership(admin, nonadmin):
    """
    The default project has created_by = NULL (no account existed when the
    migration ran). That must not accidentally deny everyone, nor grant
    everyone: admins pass, explicit members pass, strangers do not.
    """
    p = get_default_project()
    assert p.created_by_id is None
    assert user_can_see_project(admin, p) is True

    member = nonadmin
    assert user_can_see_project(member, p) is False

    ProjectMembership.objects.create(project=p, user=member, can_write=True)
    assert user_can_see_project(member, p) is True
    assert user_can_write_project(member, p) is True


def test_null_owner_invisible_to_a_stranger(nonadmin):
    """No membership and not an admin: the default project stays invisible."""
    stranger = nonadmin
    p = get_default_project()
    assert user_can_see_project(stranger, p) is False
    assert user_can_write_project(stranger, p) is False


def test_visible_projects_for_admin_includes_everything(admin, plain):
    make_project("a", plain)
    make_project("b", plain)
    assert visible_projects(admin).count() == Project.objects.count()


def test_str_representations(admin):
    p = make_project("demo", admin)
    assert str(p) == "demo (demo)"
    b = make_backlog(p)
    assert str(b) == "demo/Backlog"
    s = ChatSession.objects.create(scope=ChatScope.PROJECTS, project=p, created_by=admin)
    assert str(s).startswith("projects:")


def test_check_swallows_unexpected_database_errors(monkeypatch):
    """
    A system check must never be the reason a command fails. An unexpected
    database error becomes a warning, not a traceback.
    """
    from board import bootstrap

    def boom(*a, **k):
        raise ValueError("something unexpected")

    monkeypatch.setattr(bootstrap.Project.objects, "filter", boom)
    problems = bootstrap.default_project_exists(None)
    assert any(getattr(p, "id", "") == "board.W002" for p in problems)


def test_check_is_quiet_on_an_unmigrated_database(monkeypatch):
    from board import bootstrap
    from django.db import ProgrammingError

    def missing(*a, **k):
        raise ProgrammingError("relation does not exist")

    monkeypatch.setattr(bootstrap.Project.objects, "filter", missing)
    assert bootstrap.default_project_exists(None) == []


def test_grant_that_can_change_backlogs_works():
    """
    No current scope grants backlog changes, but the capability exists in the
    model so a future scope does not need a rewrite. Tested here so the branch
    is known-good rather than unknown.
    """
    from board.permissions import Grant

    g = Grant(scope="custom", can_change_backlogs=True)
    g.assert_can_change("backlog")
    # every other capability still defaults to refused
    with pytest.raises(PermissionError_):
        g.assert_can_change("project")
    with pytest.raises(PermissionError_):
        g.assert_can_change("task")


def test_creator_is_visible_and_writable_without_being_an_admin(nonadmin):
    """A non-admin who created a project keeps full access to it."""
    p = make_project("owned", nonadmin)
    assert user_can_see_project(nonadmin, p) is True
    assert user_can_write_project(nonadmin, p) is True


def test_creator_write_path_is_reached_without_membership(nonadmin):
    p = make_project("owned2", nonadmin)
    assert not p.memberships.exists()
    assert user_can_write_project(nonadmin, p) is True


def test_unsaved_agent_cannot_be_assigned():
    """
    An Agent instance carries a default-generated primary key, so 'unsaved'
    does not mean 'pk is None' — it means the row is not there. That is the
    case worth guarding, and it is caught by the existence check.
    """
    ghost = Agent(name="not-saved-yet")
    assert ghost.pk is not None          # the default filled it in
    assert not Agent.objects.filter(pk=ghost.pk).exists()
    assert can_assign(ghost) is False


def test_agent_with_an_explicitly_null_pk_cannot_be_assigned():
    """The defensive branch for a caller that nulls the key out."""
    assert can_assign(Agent(id=None, name="no-key")) is False
