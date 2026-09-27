"""
Tests for the bootstrap rule: the first account on a fresh install is admin.

There is no registration, so every account is created deliberately. This rule
only covers the initial bootstrap, and it must fire exactly once — an
install-wide "everyone is an admin" would be a serious flaw.
"""

import pytest
from django.contrib.auth import get_user_model

from board.models import AgentApiKey

pytestmark = pytest.mark.django_db

User = get_user_model()


def test_first_user_created_becomes_admin():
    u = User.objects.create_user("first", password="pw12345")
    u.refresh_from_db()
    assert u.is_staff is True
    assert u.is_superuser is True


def test_first_user_can_reach_the_admin_site(client):
    first = User.objects.create_user("first", password="pw12345")
    client.force_login(first)
    r = client.get("/admin/")
    assert r.status_code == 200


def test_second_user_is_not_promoted():
    User.objects.create_user("first", password="pw12345")
    second = User.objects.create_user("second", password="pw12345")
    second.refresh_from_db()
    assert second.is_staff is False
    assert second.is_superuser is False


def test_tenth_user_is_not_promoted():
    User.objects.create_user("first", password="pw12345")
    for i in range(9):
        User.objects.create_user(f"u{i}", password="pw12345")
    last = User.objects.create_user("last", password="pw12345")
    assert last.is_superuser is False


def test_explicit_staff_is_left_alone():
    u = User.objects.create_user("admin-made", password="pw12345", is_staff=True)
    u.refresh_from_db()
    assert u.is_staff is True
    assert u.is_superuser is False  # explicit is_staff must not be escalated


def test_rule_does_not_fire_on_update():
    u = User.objects.create_user("first", password="pw12345")
    u.first_name = "changed"
    u.save()
    u.refresh_from_db()
    assert u.is_superuser is True  # still admin, and no error from re-firing


def test_only_the_bootstrap_user_is_admin():
    users = [User.objects.create_user(f"u{i}", password="pw12345") for i in range(4)]
    admins = [u.username for u in users if u.is_superuser]
    assert admins == ["u0"]


def test_staff_and_superuser_are_independent_for_later_users():
    User.objects.create_user("first", password="pw12345")
    worker = User.objects.create_user("worker", password="pw12345", is_staff=True)
    worker.refresh_from_db()
    assert worker.is_staff is True
    assert worker.is_superuser is False
