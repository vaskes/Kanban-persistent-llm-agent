"""
Tests for self-service registration.

Two rules are being pinned down here, and they pull in opposite directions:

  * the first account on a fresh instance becomes its administrator;
  * every later account sees nothing at all until an administrator grants it
    access to a project.

The first is a bootstrap convenience. The second is the whole point: signing
up must not be a way to see work you were not given.
"""

import pytest
from django.contrib.auth import get_user_model

from board.forms import RegistrationForm, new_user_sees_nothing, register_user
from board.permissions import user_can_see_project, visible_projects

pytestmark = pytest.mark.django_db

User = get_user_model()


def payload(username="newcomer", password="Sup3rSecret!42", email=""):
    return {
        "username": username,
        "email": email,
        "password1": password,
        "password2": password,
    }


# --------------------------------------------------------------------------
# the page and the form
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_register_page_renders(client):
    r = client.get("/register/")
    assert r.status_code == 200
    body = r.content.decode()
    for phrase in ("Create an account", "Username", "Email", "Password",
                   "Confirm password", "Create account"):
        assert phrase in body, f"missing {phrase!r}"
    assert "csrfmiddlewaretoken" in body


@pytest.mark.django_db
def test_register_page_has_exactly_one_input_per_field(client):
    """
    The same class of bug as the login page: a rendered bound field emits its
    own input and label, which duplicated the username box.
    """
    import re

    body = client.get("/register/").content.decode()
    assert len(re.findall(r'name="username"', body)) == 1
    assert len(re.findall(r'name="password1"', body)) == 1
    assert len(re.findall(r'name="password2"', body)) == 1
    assert "form.username" not in body


@pytest.mark.django_db
def test_register_page_explains_the_bootstrap_rule(client):
    body = client.get("/register/").content.decode()
    assert "first account" in body.lower()
    assert "administrator" in body.lower()
    assert "grants" in body.lower()


@pytest.mark.django_db
def test_register_page_is_in_english(client):
    body = client.get("/register/").content.decode()
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body)


def test_email_is_optional():
    form = RegistrationForm(data=payload())
    assert form.is_valid(), form.errors


def test_username_must_be_unique():
    User.objects.create_user("taken", password="pw12345678")
    form = RegistrationForm(data=payload(username="taken"))
    assert not form.is_valid()
    assert "username" in form.errors


def test_mismatched_passwords_are_refused():
    data = payload()
    data["password2"] = "something-else-99"
    form = RegistrationForm(data=data)
    assert not form.is_valid()


def test_weak_password_is_refused():
    data = payload(password="123")
    form = RegistrationForm(data=data)
    assert not form.is_valid()


def test_invalid_username_characters_refused():
    form = RegistrationForm(data=payload(username="has space"))
    assert not form.is_valid()


# --------------------------------------------------------------------------
# the first account
# --------------------------------------------------------------------------


def test_first_registered_user_becomes_administrator():
    user, is_first = register_user(RegistrationForm(data=payload("boss")))
    assert is_first is True
    assert user.is_superuser is True
    assert user.is_staff is True


def test_second_registered_user_is_ordinary():
    register_user(RegistrationForm(data=payload("boss")))
    user, is_first = register_user(RegistrationForm(data=payload("second")))
    assert is_first is False
    assert user.is_superuser is False
    assert user.is_staff is False


def test_bootstrap_admin_happens_exactly_once():
    register_user(RegistrationForm(data=payload("a")))
    register_user(RegistrationForm(data=payload("b")))
    register_user(RegistrationForm(data=payload("c")))
    admins = list(User.objects.filter(is_superuser=True).values_list("username", flat=True))
    assert admins == ["a"]


def test_register_user_reports_first_only_for_the_very_first():
    _, f1 = register_user(RegistrationForm(data=payload("one")))
    _, f2 = register_user(RegistrationForm(data=payload("two")))
    assert [f1, f2] == [True, False]


# --------------------------------------------------------------------------
# a new user sees nothing
# --------------------------------------------------------------------------


def test_new_user_sees_no_projects():
    register_user(RegistrationForm(data=payload("boss")))
    user, _ = register_user(RegistrationForm(data=payload("stranger")))
    assert visible_projects(user).count() == 0
    assert new_user_sees_nothing(user) is True


def test_new_user_cannot_see_the_default_project():
    """
    The default project is not granted implicitly. An account that could read
    it would not be an empty one, which is exactly what signup must not give.
    """
    from board.bootstrap import get_default_project

    register_user(RegistrationForm(data=payload("boss")))
    user, _ = register_user(RegistrationForm(data=payload("stranger")))
    assert user_can_see_project(user, get_default_project()) is False


def test_first_admin_does_see_everything():
    """The bootstrap administrator is not an empty account."""
    user, _ = register_user(RegistrationForm(data=payload("boss")))
    assert visible_projects(user).count() >= 1
    assert new_user_sees_nothing(user) is False


def test_granting_membership_makes_a_project_visible():
    from board.bootstrap import get_default_project
    from board.models import ProjectMembership

    register_user(RegistrationForm(data=payload("boss")))
    user, _ = register_user(RegistrationForm(data=payload("stranger")))
    assert new_user_sees_nothing(user) is True

    ProjectMembership.objects.create(
        project=get_default_project(), user=user, can_write=False
    )
    assert new_user_sees_nothing(user) is False
    assert user_can_see_project(user, get_default_project()) is True


# --------------------------------------------------------------------------
# through HTTP
# --------------------------------------------------------------------------


def test_registration_creates_and_signs_in(client):
    r = client.post("/register/", payload("webuser"), follow=True)
    assert r.status_code == 200
    assert User.objects.filter(username="webuser").exists()
    # signed in without a second login
    assert client.session.get("_auth_user_id") is not None
    # and the board is reachable
    assert client.get("/").status_code == 200


def test_first_registration_over_http_is_the_admin(client):
    client.post("/register/", payload("founder"), follow=True)
    u = User.objects.get(username="founder")
    assert u.is_superuser is True


def test_duplicate_registration_rejected(client):
    User.objects.create_user("dupe", password="pw12345678")
    r = client.post("/register/", payload("dupe"))
    assert r.status_code == 200
    assert not User.objects.filter(username="dupe").count() > 1
    assert "already exists" in r.content.decode().lower()


def test_failed_registration_keeps_the_username(client):
    r = client.post("/register/", payload("keeper", password="123"))
    body = r.content.decode()
    assert 'value="keeper"' in body
    assert client.session.get("_auth_user_id") is None


def test_authenticated_user_is_redirected_away_from_register(client, django_user_model):
    u = django_user_model.objects.create_user("already", password="pw12345678")
    client.force_login(u)
    r = client.get("/register/")
    assert r.status_code == 302
    assert r["Location"] == "/"


def test_login_page_links_to_registration(client):
    body = client.get("/login/").content.decode()
    assert 'href="/register/"' in body
    assert "Create one" in body
    assert "no sign-up" not in body.lower()


# --------------------------------------------------------------------------
# the empty state
# --------------------------------------------------------------------------


def test_board_explains_a_new_account_sees_nothing(client):
    register_user(RegistrationForm(data=payload("boss")))
    user, _ = register_user(RegistrationForm(data=payload("stranger")))
    client.force_login(user)
    body = client.get("/").content.decode()
    assert "No projects yet" in body
    assert "grant you read access" in body


def test_board_of_an_ordinary_member_is_not_empty(client):
    from board.bootstrap import get_default_project
    from board.models import ProjectMembership

    register_user(RegistrationForm(data=payload("boss")))
    user, _ = register_user(RegistrationForm(data=payload("member")))
    ProjectMembership.objects.create(
        project=get_default_project(), user=user, can_write=False
    )
    client.force_login(user)
    body = client.get("/").content.decode()
    assert "No projects yet" not in body
