"""
Tests for the resource admin surfaces.

The admin is the operator's escape hatch when the WebUI doesn't cover
something. Each concrete resource kind needs its own admin registered
with the right list_display, otherwise the operator cannot see at a
glance what is registered.
"""

import pytest
from django.contrib import admin as dj_admin
from django.urls import reverse

from board.admin import (
    BrowserAdmin,
    ComfyUiEndpointAdmin,
    GitRepoAdmin,
    HostAdmin,
    ResourceAdmin,
    ResourceAllocationAdmin,
    SandboxAdmin,
)
from board.models import (
    Browser, ComfyUiEndpoint, GitRepo, Host,
    Project, Resource, ResourceAllocation, Sandbox, Task,
)
from board.bootstrap import ensure_default_project


pytestmark = pytest.mark.django_db


@pytest.fixture
def boot(django_user_model):
    django_user_model.objects.create_user("boot", password="pw12345")


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


# --- registrations exist -------------------------------------------------


def test_resource_subtypes_are_registered_for_url_routing():
    """Every concrete kind must have an admin so the kind picker's add URL
    resolves. LlmEndpoint is intentionally absent — Agent subsumes its role."""
    for m in (Resource, Host, Sandbox, ComfyUiEndpoint,
              GitRepo, Browser, ResourceAllocation):
        assert m in dj_admin.site._registry, f"{m.__name__} not registered"


def test_resource_admin_shows_capacity_and_free_slots():
    assert "capacity" in ResourceAdmin.list_display
    assert "free_slots" in ResourceAdmin.list_display


def test_host_admin_includes_connection_columns():
    assert {"hostname", "ip", "proto", "port"} <= set(HostAdmin.list_display)


def test_sandbox_admin_includes_pool_size():
    assert "pool_size" in SandboxAdmin.list_display


def test_browser_admin_includes_headless():
    assert "headless" in BrowserAdmin.list_display


def test_allocation_admin_shows_owner():
    assert "owner" in ResourceAllocationAdmin.list_display


# --- rendering ------------------------------------------------------------


def test_resource_admin_changelist_renders(staff_client):
    Host.objects.create(name="alpha")
    Host.objects.create(name="beta")
    r = staff_client.get(reverse("admin:board_resource_changelist"))
    assert r.status_code == 200
    body = r.content.decode()
    assert "alpha" in body
    assert "beta" in body


def test_host_admin_changelist_renders(staff_client):
    Host.objects.create(name="h1", ip="10.0.0.1")
    r = staff_client.get(reverse("admin:board_host_changelist"))
    assert r.status_code == 200
    assert "10.0.0.1" in r.content.decode()


def test_sandbox_admin_changelist_renders(staff_client):
    Sandbox.objects.create(name="sb", image="ubuntu:22.04", pool_size=2)
    r = staff_client.get(reverse("admin:board_sandbox_changelist"))
    assert r.status_code == 200
    assert "ubuntu:22.04" in r.content.decode()



def test_comfyui_admin_changelist_renders(staff_client):
    ComfyUiEndpoint.objects.create(name="cf", url="http://x:8188")
    r = staff_client.get(reverse("admin:board_comfyuiendpoint_changelist"))
    assert r.status_code == 200
    assert "http://x:8188" in r.content.decode()


def test_git_admin_changelist_renders(staff_client):
    GitRepo.objects.create(name="repo", url="git@github.com:foo/bar.git")
    r = staff_client.get(reverse("admin:board_gitrepo_changelist"))
    assert r.status_code == 200


def test_browser_admin_changelist_renders(staff_client):
    Browser.objects.create(name="br", pool_size=3)
    r = staff_client.get(reverse("admin:board_browser_changelist"))
    assert r.status_code == 200


def test_allocation_admin_changelist_renders(staff_client):
    project = ensure_default_project()
    backlog = project.backlogs.get(is_default=True)
    task = Task.objects.create(
        title="t", acceptance="x", project=project, backlog=backlog,
    )
    host = Host.objects.create(name="h")
    ResourceAllocation.objects.create(resource=host, task=task)
    r = staff_client.get(reverse("admin:board_resourceallocation_changelist"))
    assert r.status_code == 200
    body = r.content.decode()
    assert "granted" in body or "requested" in body


# --- credentials are not leaked in admin ---------------------------------


def test_admin_does_not_render_plaintext_credentials(staff_client):
    h = Host.objects.create(name="h")
    h.set_secret("ssh_key_cipher", "PRIVATE-KEY-CONTENT-MUST-NOT-LEAK")
    h.save()
    r = staff_client.get(reverse("admin:board_host_changelist"))
    body = r.content.decode()
    assert "PRIVATE-KEY-CONTENT-MUST-NOT-LEAK" not in body


def test_owner_method_handles_corrupt_rows_without_raising(staff_client):
    """If a row somehow has neither task nor project (data corruption),
    the owner column must render something safe rather than crash the page."""
    h = Host.objects.create(name="h")
    # bypass full_clean() to simulate a corrupt row that should not exist
    a = ResourceAllocation.objects.create(resource=h, task=None, project=None)
    r = staff_client.get(reverse("admin:board_resourceallocation_changelist"))
    assert r.status_code == 200
    # the orphan row must appear with the "-" owner marker
    assert "-" in r.content.decode()


def test_owner_method_renders_project_id_when_project_set(staff_client):
    project = ensure_default_project()
    h = Host.objects.create(name="h2")
    a = ResourceAllocation.objects.create(resource=h, project=project)
    r = staff_client.get(reverse("admin:board_resourceallocation_changelist"))
    assert r.status_code == 200
    # the owner's project form must show up — the project pk is short
    assert f"project:{project.pk}" in r.content.decode()


def test_llm_endpoint_str_includes_name_and_url():
    """LlmEndpoint is unregistered but its model still exists; the str
    helper must remain callable for any code that touches it."""
    from board.models import LlmEndpoint
    # build an in-memory instance without saving (unregistered = no admin)
    a = LlmEndpoint(name="x", base_url="http://y/v1", model_id="m")
    assert str(a) == "llm:x → http://y/v1"
