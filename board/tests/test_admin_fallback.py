"""Coverage for the Resource admin's defensive fall-through branches.

The redirect helper returns None when the row's `kind` is not in
`_KIND_TO_MODEL` — that can only happen on a corrupt row (every
concrete save() self-declares a valid kind). These tests force the
corrupt state with a queryset .update() so the fallback is exercised.
"""

import uuid

import pytest
from django.urls import reverse

from board.models import Resource

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(django_user_model):
    return django_user_model.objects.create_user(
        "staff", password="pw", is_staff=True, is_superuser=True,
    )


@pytest.fixture
def staff_client(staff):
    from django.test import Client
    c = Client()
    c.force_login(staff)
    return c


@pytest.fixture
def corrupt_host():
    """A Host row whose kind column was corrupted to a value no model claims."""
    r = Resource.objects.create(name="corrupt-host")
    Resource.objects.filter(pk=r.pk).update(kind="not_a_real_kind")
    r.refresh_from_db()
    assert r.kind == "not_a_real_kind"
    return r


def test_configure_url_renders_empty_for_unknown_kind(corrupt_host):
    """configure_url must not raise or emit a broken link for a corrupt row."""
    from django.contrib import admin as dj_admin
    model_admin = dj_admin.site._registry[Resource]
    assert model_admin.configure_url(corrupt_host) == ""


def test_configure_url_renders_link_for_valid_kind(staff_client):
    """Sanity: the same method returns an <a href> for a healthy row."""
    from django.contrib import admin as dj_admin
    model_admin = dj_admin.site._registry[Resource]
    r = Resource.objects.create(name="healthy")
    Resource.objects.filter(pk=r.pk).update(kind="host")
    r.refresh_from_db()
    out = model_admin.configure_url(r)
    assert out.startswith('<a href=')
    assert "configure" in out


def test_change_view_falls_through_to_super_for_corrupt_kind(staff_client, corrupt_host):
    """No subtype to redirect to → super() renders the base Resource form."""
    r = staff_client.get(
        reverse("admin:board_resource_change", args=[corrupt_host.pk]),
    )
    assert r.status_code == 200


def test_delete_view_falls_through_to_super_for_corrupt_kind(staff_client, corrupt_host):
    """No subtype to redirect to → super() renders the base delete confirm."""
    r = staff_client.get(
        reverse("admin:board_resource_delete", args=[corrupt_host.pk]),
    )
    assert r.status_code == 200


def test_history_view_falls_through_to_super_for_corrupt_kind(staff_client, corrupt_host):
    """No subtype to redirect to → super() renders the base history page."""
    r = staff_client.get(
        reverse("admin:board_resource_history", args=[corrupt_host.pk]),
    )
    assert r.status_code == 200


def test_redirect_helper_returns_none_when_object_missing(staff, staff_client):
    """get_object() returns None for an unknown pk → helper returns None and
    super() renders the 404, rather than raising AttributeError."""
    from django.contrib import admin as dj_admin
    from django.test import RequestFactory

    model_admin = dj_admin.site._registry[Resource]
    rf = RequestFactory()
    request = rf.get("/")
    request.user = staff

    missing = uuid.uuid4()
    assert model_admin._redirect_to_subtype(request, missing, "change", None) is None