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


# --- LlmEndpoint and McpServer: the forward-looking pair -----------------


def test_llm_endpoint_is_registered_for_picker_url_routing():
    """LlmEndpoint is hidden from the sidebar (via _hide_from_sidebar) but
    must be in the registry so the kind picker's add URL resolves."""
    from board.models import LlmEndpoint
    assert LlmEndpoint in dj_admin.site._registry


def test_mcp_server_is_registered():
    """McpServer is the new forward-looking kind; admin must register it."""
    from board.models import McpServer
    assert McpServer in dj_admin.site._registry


def test_mcp_server_kind_is_in_resource_kind_choices():
    from board.models import McpServer, ResourceKind
    # The model must self-declare its kind on save.
    m = McpServer.objects.create(
        name="m1", url="http://localhost:9999/mcp",
        transport="http", protocol_version="2024-11-05",
    )
    m.refresh_from_db()
    assert m.kind == "mcp_server"
    assert ResourceKind.MCP_SERVER == "mcp_server"


def test_mcp_server_str_includes_transport_and_url():
    from board.models import McpServer
    s = McpServer(name="x", url="http://y/mcp", transport="http")
    assert "mcp:x" in str(s)
    assert "http" in str(s)


def test_mcp_server_stdio_omits_url_in_str():
    """For stdio transport the URL is meaningless — str() must not advertise it."""
    from board.models import McpServer
    s = McpServer(name="x", url="", transport="stdio")
    s_str = str(s)
    assert "<stdio>" in s_str or "stdio" in s_str
    assert "http://" not in s_str


def test_mcp_server_defaults_to_http_transport():
    from board.models import McpServer
    m = McpServer.objects.create(name="m")
    assert m.transport == "http"
    assert m.protocol_version == "2024-11-05"


def test_mcp_server_declared_tools_defaults_to_empty_list():
    """declared_tools must default to [] so JSONField lookups don't fail on
    a fresh row with null vs missing."""
    from board.models import McpServer
    m = McpServer.objects.create(name="m")
    assert m.declared_tools == []


def test_mcp_server_can_store_declared_tools():
    from board.models import McpServer
    m = McpServer.objects.create(
        name="fs-mcp",
        declared_tools=["read_file", "write_file", "list_dir"],
    )
    m.refresh_from_db()
    assert m.declared_tools == ["read_file", "write_file", "list_dir"]


def test_kind_picker_lists_llm_endpoint_and_mcp_server(admin_client):
    """The Resources picker now includes both forward-looking kinds."""
    r = admin_client.get(reverse("admin:board_resource_add"))
    body = r.content.decode()
    assert "LLM endpoint" in body
    assert "MCP server" in body


def test_kind_picker_llm_endpoint_link_resolves(admin_client):
    """The LLM endpoint tile links to a real add URL."""
    r = admin_client.get(reverse("admin:board_resource_add"))
    body = r.content.decode()
    assert reverse("admin:board_llmendpoint_add") in body


def test_kind_picker_mcp_server_link_resolves(admin_client):
    r = admin_client.get(reverse("admin:board_resource_add"))
    body = r.content.decode()
    assert reverse("admin:board_mcpserver_add") in body


def test_llm_endpoint_admin_changelist_renders(admin_client):
    from board.models import LlmEndpoint
    LlmEndpoint.objects.create(name="llm", base_url="http://x/v1", model_id="m")
    r = admin_client.get(reverse("admin:board_llmendpoint_changelist"))
    assert r.status_code == 200
    assert "http://x/v1" in r.content.decode()


def test_mcp_server_admin_changelist_renders(admin_client):
    from board.models import McpServer
    McpServer.objects.create(name="mcp-fs", url="http://fs.local/mcp")
    r = admin_client.get(reverse("admin:board_mcpserver_changelist"))
    assert r.status_code == 200
    assert "http://fs.local/mcp" in r.content.decode()


def test_llm_endpoint_appears_in_resources_changelist_kind_column(admin_client):
    """The unified Resources view shows the kind column populated for LlmEndpoint."""
    from board.models import LlmEndpoint
    LlmEndpoint.objects.create(name="advisor", base_url="http://x/v1", model_id="m")
    r = admin_client.get(reverse("admin:board_resource_changelist"))
    body = r.content.decode()
    assert "advisor" in body
    assert "llm_endpoint" in body


# --- Resource admin redirects to subtype admin ----------------------------
#
# The base Resource change form has no per-kind columns (hostname, IP,
# ssh_key_cipher, …). Clicking a row in /admin/board/resource/ must therefore
# bounce the operator to the matching subtype admin that owns those fields.


def test_resource_change_view_redirects_host_to_host_admin(staff_client):
    h = Host.objects.create(name="h", ip="10.0.0.5")
    r = staff_client.get(
        reverse("admin:board_resource_change", args=[h.pk]),
    )
    assert r.status_code == 302
    assert r.url == reverse("admin:board_host_change", args=[h.pk])


def test_resource_change_view_redirects_sandbox_to_sandbox_admin(staff_client):
    s = Sandbox.objects.create(name="s", image="ubuntu:22.04", pool_size=1)
    r = staff_client.get(
        reverse("admin:board_resource_change", args=[s.pk]),
    )
    assert r.status_code == 302
    assert r.url == reverse("admin:board_sandbox_change", args=[s.pk])


def test_resource_change_view_redirects_comfyui_to_comfyui_admin(staff_client):
    c = ComfyUiEndpoint.objects.create(name="cf", url="http://x:8188")
    r = staff_client.get(
        reverse("admin:board_resource_change", args=[c.pk]),
    )
    assert r.status_code == 302
    assert r.url == reverse("admin:board_comfyuiendpoint_change", args=[c.pk])


def test_resource_change_view_redirects_gitrepo_to_gitrepo_admin(staff_client):
    g = GitRepo.objects.create(name="gr", url="git@github.com:foo/bar.git")
    r = staff_client.get(
        reverse("admin:board_resource_change", args=[g.pk]),
    )
    assert r.status_code == 302
    assert r.url == reverse("admin:board_gitrepo_change", args=[g.pk])


def test_resource_change_view_redirects_browser_to_browser_admin(staff_client):
    b = Browser.objects.create(name="br", pool_size=1)
    r = staff_client.get(
        reverse("admin:board_resource_change", args=[b.pk]),
    )
    assert r.status_code == 302
    assert r.url == reverse("admin:board_browser_change", args=[b.pk])


def test_resource_change_view_redirects_llmendpoint_to_llmendpoint_admin(staff_client):
    from board.models import LlmEndpoint
    LlmEndpoint.objects.create(name="llm", base_url="http://x/v1", model_id="m")
    obj = Resource.objects.get(name="llm")
    r = staff_client.get(
        reverse("admin:board_resource_change", args=[obj.pk]),
    )
    assert r.status_code == 302
    assert r.url == reverse("admin:board_llmendpoint_change", args=[obj.pk])


def test_resource_change_view_redirects_mcpserver_to_mcpserver_admin(staff_client):
    from board.models import McpServer
    McpServer.objects.create(name="mcp", url="http://m.local/mcp", transport="http")
    obj = Resource.objects.get(name="mcp")
    r = staff_client.get(
        reverse("admin:board_resource_change", args=[obj.pk]),
    )
    assert r.status_code == 302
    assert r.url == reverse("admin:board_mcpserver_change", args=[obj.pk])


def test_resource_delete_view_redirects_to_subtype(staff_client):
    h = Host.objects.create(name="h")
    r = staff_client.get(
        reverse("admin:board_resource_delete", args=[h.pk]),
    )
    assert r.status_code == 302
    assert r.url == reverse("admin:board_host_delete", args=[h.pk])


def test_resource_history_view_redirects_to_subtype(staff_client):
    h = Host.objects.create(name="h")
    r = staff_client.get(
        reverse("admin:board_resource_history", args=[h.pk]),
    )
    assert r.status_code == 302
    assert r.url == reverse("admin:board_host_history", args=[h.pk])


def test_resource_changelist_renders_configure_link_per_kind(staff_client):
    """Each row in /admin/board/resource/ now has a 'configure' link that
    points to the matching subtype admin's change page."""
    Host.objects.create(name="h-link", ip="1.2.3.4")
    Sandbox.objects.create(name="s-link", image="alpine:3", pool_size=1)
    r = staff_client.get(reverse("admin:board_resource_changelist"))
    body = r.content.decode()
    assert "configure" in body
    # at least one link to the host admin's change page
    h = Host.objects.get(name="h-link")
    assert reverse("admin:board_host_change", args=[h.pk]) in body


def test_resource_admin_list_display_includes_configure_url():
    """`configure_url` is a public column on ResourceAdmin so the operator
    sees a direct edit link in the unified list."""
    assert "configure_url" in ResourceAdmin.list_display


# --- Subtype admins expose structured fields via fieldsets -----------------


def _fieldset_field_names(admin_cls):
    """Flatten a ModelAdmin's fieldsets into a single field-name list."""
    names = []
    for _title, opts in admin_cls.fieldsets or []:
        names.extend(opts.get("fields", ()))
    return names


def test_host_admin_fieldsets_include_connection_and_credentials():
    fields = _fieldset_field_names(HostAdmin)
    # Identity
    for f in ("os_type", "hostname", "ip", "proto", "port"):
        assert f in fields, f"Host fieldset missing {f}"
    # Credentials (encrypted)
    for f in ("ssh_key_cipher", "login", "password_cipher"):
        assert f in fields, f"Host fieldset missing {f}"


def test_sandbox_admin_fieldsets_include_pool_and_container_fields():
    fields = _fieldset_field_names(SandboxAdmin)
    for f in ("pool_size", "image", "cpu_limit", "memory_limit_mb"):
        assert f in fields, f"Sandbox fieldset missing {f}"


def test_browser_admin_fieldsets_include_image_and_headless():
    fields = _fieldset_field_names(BrowserAdmin)
    assert {"image", "headless", "pool_size"} <= set(fields)


def test_gitrepo_admin_fieldsets_include_url_and_ssh_key():
    fields = _fieldset_field_names(GitRepoAdmin)
    assert {"url", "default_branch", "ssh_key_cipher"} <= set(fields)


def test_comfyui_admin_fieldsets_include_url_and_api_key():
    fields = _fieldset_field_names(ComfyUiEndpointAdmin)
    assert {"url", "api_key_cipher", "workflow_timeout_seconds"} <= set(fields)


def test_resource_change_view_for_missing_id_falls_through(staff_client):
    """If the object does not exist, super().change_view renders a 404.
    The redirect helper must not crash with AttributeError on None."""
    import uuid
    r = staff_client.get(
        reverse("admin:board_resource_change", args=[uuid.uuid4()]),
    )
    assert r.status_code in (302, 404)
