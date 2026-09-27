"""
Tests for the four-section admin sidebar.

The operator-facing admin has exactly four sections — Human Resources,
Projects, Agent resources, Usable Resources. Anything else (Tasks, audit
trails, chat history, resource subtype changelists) is hidden from the
sidebar but its admin URLs still work for technical inspection.

These tests pin:

- The four sections appear, in the right order.
- Hidden models (Host, Sandbox, …) are NOT in any sidebar section.
- Hidden models' admin URLs still return 200 (the operator can reach them
  directly when they need to).
- Hidden models DO appear in the registry (so the kind picker can resolve
  their add URLs).
- The Resources changelist add URL shows the kind picker, not a broken form.
"""

import pytest
from django.contrib import admin as dj_admin
from django.test import Client
from django.urls import reverse

from board.models import (
    Agent,
    Attempt,
    Browser,
    ChatMessage,
    ChatSession,
    ComfyUiEndpoint,
    GitRepo,
    Host,
    Memory,
    Project,
    Resource,
    ResourceAllocation,
    Sandbox,
    Task,
    TaskDep,
    TaskEvent,
)
from board.bootstrap import ensure_default_project


pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(django_user_model):
    """First user becomes admin via the promote_first_user signal."""
    return django_user_model.objects.create_user("boss", password="pw")


@pytest.fixture
def admin_client(admin):
    c = Client()
    c.force_login(admin)
    return c


def _sidebar_sections(client):
    """Return the list of section names rendered on the admin index."""
    r = client.get(reverse("admin:index"))
    assert r.status_code == 200
    body = r.content.decode()
    # The index page renders each section's <caption> with the section name.
    import re
    return re.findall(r'<caption[^>]*>\s*<a[^>]*>([^<]+)</a>', body)


# --- sidebar shape ---------------------------------------------------------


def test_admin_index_renders_all_four_sections(admin_client):
    sections = _sidebar_sections(admin_client)
    assert sections == [
        "Human Resources",
        "Projects",
        "Agent resources",
        "Usable Resources",
    ]


def test_admin_index_has_no_section_beyond_the_four(admin_client):
    sections = _sidebar_sections(admin_client)
    assert len(sections) == 4


def test_index_title_is_not_legacy_board_data(admin_client):
    body = admin_client.get(reverse("admin:index")).content.decode()
    assert "Board data" not in body


# --- what's hidden ---------------------------------------------------------


@pytest.mark.parametrize("model", [
    Task, TaskEvent, TaskDep, Attempt, Memory, ChatSession, ChatMessage,
])
def test_internal_models_are_not_registered_in_admin(model):
    """
    Tasks, audit logs, chat history are runtime state. The operator sees
    them through the WebUI board / project pages, not the admin sidebar.
    """
    assert model not in dj_admin.site._registry


def test_groups_model_is_not_registered():
    """Flat hierarchy; no Django Groups in this project."""
    from django.contrib.auth.models import Group
    assert Group not in dj_admin.site._registry


def test_llm_endpoint_is_registered_but_hidden():
    """LlmEndpoint is a forward-looking kind (consult / delegate to a separate
    LLM from an agent). It IS registered — the kind picker needs its add URL —
    but hidden from the sidebar via _hide_from_sidebar."""
    from board.models import LlmEndpoint
    assert LlmEndpoint in dj_admin.site._registry
    ma = dj_admin.site._registry[LlmEndpoint]
    assert ma.has_module_permission(None) is False


@pytest.mark.parametrize("model", [
    Host, Sandbox, ComfyUiEndpoint, GitRepo, Browser,
])
def test_resource_subtype_admins_are_registered_but_hidden(model):
    """
    The subtype admin IS in the registry (URLs work), but its
    has_module_permission returns False so the sidebar skips it. Resources
    is the operator's entry point; the subtypes are reachable only via the
    kind picker or direct URL.
    """
    assert model in dj_admin.site._registry
    ma = dj_admin.site._registry[model]
    # has_module_permission must be False for it to be filtered out
    assert ma.has_module_permission(None) is False


def test_resource_allocations_are_registered_but_hidden():
    assert ResourceAllocation in dj_admin.site._registry
    ma = dj_admin.site._registry[ResourceAllocation]
    assert ma.has_module_permission(None) is False


# --- what's in each section -----------------------------------------------


def test_human_resources_section_has_only_users(admin_client):
    """No Groups, no Project memberships — flat operator hierarchy."""
    body = admin_client.get(reverse("admin:index")).content.decode()
    # Users is the only model in the Human Resources section.
    # Locate the section by anchor and check its first model name.
    import re
    m = re.search(
        r'Human Resources.*?</table>',
        body, flags=re.DOTALL,
    )
    assert m is not None, "Human Resources section missing"
    section_body = m.group(0)
    assert "Users" in section_body
    assert "Groups" not in section_body
    assert "Project memberships" not in section_body


def test_projects_section_has_only_projects(admin_client):
    import re
    body = admin_client.get(reverse("admin:index")).content.decode()
    m = re.search(
        r'Projects.*?</table>',
        body, flags=re.DOTALL,
    )
    assert m is not None, "Projects section missing"
    section_body = m.group(0)
    assert "Projects" in section_body
    assert "Project memberships" not in section_body
    assert "Backlogs" not in section_body


def test_agent_resources_section_has_only_agents(admin_client):
    """LLM endpoints and agent API keys are NOT separate sidebar entries."""
    import re
    body = admin_client.get(reverse("admin:index")).content.decode()
    m = re.search(
        r'Agent resources.*?</table>',
        body, flags=re.DOTALL,
    )
    assert m is not None, "Agent resources section missing"
    section_body = m.group(0)
    assert "Agents" in section_body
    assert "Agent api keys" not in section_body
    assert "Llm endpoints" not in section_body


def test_usable_resources_section_has_only_resources(admin_client):
    """Subtype changelists must NOT appear in this section."""
    import re
    body = admin_client.get(reverse("admin:index")).content.decode()
    m = re.search(
        r'Usable Resources.*?</table>',
        body, flags=re.DOTALL,
    )
    assert m is not None, "Usable Resources section missing"
    section_body = m.group(0)
    assert "Resources" in section_body
    assert "Hosts" not in section_body
    assert "Sandboxes" not in section_body
    assert "Comfy ui endpoints" not in section_body
    assert "Git repos" not in section_body
    assert "Browsers" not in section_body
    assert "Resource allocations" not in section_body


# --- hidden URLs still work ------------------------------------------------


@pytest.mark.parametrize("model,url_name", [
    (Host, "admin:board_host_changelist"),
    (Sandbox, "admin:board_sandbox_changelist"),
    (Browser, "admin:board_browser_changelist"),
    (GitRepo, "admin:board_gitrepo_changelist"),
    (ComfyUiEndpoint, "admin:board_comfyuiendpoint_changelist"),
])
def test_hidden_subtype_changelist_urls_are_reachable(admin_client, model, url_name):
    """Hiding from sidebar != removing the URL. Operators need to reach
    these via direct URL when the picker links through."""
    r = admin_client.get(reverse(url_name))
    assert r.status_code == 200, f"{url_name} broken: {r.status_code}"


def test_resource_allocations_changelist_url_is_reachable(admin_client):
    r = admin_client.get(reverse("admin:board_resourceallocation_changelist"))
    assert r.status_code == 200


# --- the kind picker -------------------------------------------------------


def test_resources_add_view_renders_the_kind_picker(admin_client):
    """Adding a resource must go through a kind picker, not a broken form."""
    r = admin_client.get(reverse("admin:board_resource_add"))
    assert r.status_code == 200
    body = r.content.decode()
    assert "Add a resource" in body
    # one button per available kind
    for label in ("Host", "Sandbox", "ComfyUI endpoint",
                  "Git repository", "Web browser"):
        assert label in body, f"missing kind {label!r} in picker"


def test_kind_picker_links_to_each_subtype_admin(admin_client):
    """Each button in the picker must point at a real admin add URL."""
    r = admin_client.get(reverse("admin:board_resource_add"))
    body = r.content.decode()
    for url_name in (
        "admin:board_host_add",
        "admin:board_sandbox_add",
        "admin:board_comfyuiendpoint_add",
        "admin:board_gitrepo_add",
        "admin:board_browser_add",
    ):
        assert url_name.split(":")[1] in body or reverse(url_name) in body


def test_going_through_the_picker_actually_creates_a_resource(admin_client):
    """End-to-end: pick a kind, fill the form, see the row in Resources."""
    # Step 1: open the picker
    r = admin_client.get(reverse("admin:board_resource_add"))
    assert r.status_code == 200
    # Step 2: follow the Host Add URL with form data. kind + lifetime are
    # required by the parent Resource model — Host.save() auto-fills them
    # in code, but the admin form still asks for them.
    add_url = reverse("admin:board_host_add")
    r = admin_client.post(add_url, {
        "name": "box-1",
        "kind": "host",
        "lifetime": "persistent",
        "os_type": "linux",
        "hostname": "box.local",
        "ip": "10.0.0.5",
        "proto": "ssh",
        "port": "22",
        "description": "",
        "capacity": "1",
        "_save": "Save",
    })
    assert r.status_code in (200, 302)
    # And the row exists, joined to the parent Resource table.
    assert Host.objects.filter(name="box-1").exists()
    assert Resource.objects.filter(name="box-1", kind="host").exists()


# --- coverage: the helper branches the happy path skips -----------------


def test_resources_add_view_post_falls_back_to_default_form(admin_client):
    """The picker handles GET; if someone POSTs to it without a `kind`
    field, we render the default form (which will fail validation, but
    not crash). This is the safety net for direct POSTs."""
    r = admin_client.post(reverse("admin:board_resource_add"), {})
    assert r.status_code == 200


def test_double_unregister_raises_but_can_be_caught():
    """admin.site.unregister is not idempotent — a second call raises.
    This is the property the import-time try/except blocks in board/admin.py
    rely on so that re-running the module is safe."""
    from django.contrib import admin as dj_admin
    from django.contrib.auth.models import Group
    with pytest.raises(dj_admin.sites.NotRegistered):
        dj_admin.site.unregister(Group)


def test_get_app_list_skips_section_when_model_not_in_registry(admin):
    """If a SECTION_MODELS entry isn't registered, that section is omitted
    rather than raising KeyError."""
    from django.contrib import admin as dj_admin
    from django.test import RequestFactory
    from board.admin import _kanban_get_app_list

    real_registry = dict(dj_admin.site._registry)

    class _NoAgentSite:
        _registry = {k: v for k, v in real_registry.items() if k is not Agent}

    rf = RequestFactory()
    req = rf.get("/admin/")
    req.user = admin
    sections = _kanban_get_app_list(_NoAgentSite(), req)
    labels = [s["name"] for s in sections]
    assert "Agent resources" not in labels, sections


def test_get_app_list_skips_when_module_permission_false(admin):
    """A model whose has_module_permission returns False is skipped —
    the corresponding section either stays without it, or is omitted if
    that was its only model. We patch Project (which normally returns True)
    so the True branch of the if-statement is actually exercised."""
    from django.contrib import admin as dj_admin
    from django.test import RequestFactory
    from board.admin import _kanban_get_app_list

    real = dj_admin.site._registry[Project].has_module_permission
    dj_admin.site._registry[Project].has_module_permission = lambda r: False
    try:
        rf = RequestFactory()
        req = rf.get("/admin/")
        req.user = admin
        sections = _kanban_get_app_list(dj_admin.site, req)
        labels = [s["name"] for s in sections]
        assert "Projects" not in labels, labels
    finally:
        dj_admin.site._registry[Project].has_module_permission = real


def test_get_app_list_handles_missing_registry_entries():
    """If a model in SECTION_MODELS is not registered, that section is
    omitted (rather than raising KeyError)."""
    # Build a minimal registry and call the override directly.
    from board.admin import _kanban_get_app_list
    from types import SimpleNamespace

    fake_request = SimpleNamespace(user=SimpleNamespace(is_active=True))

    class _Site:
        _registry = {}  # empty

    sections = _kanban_get_app_list(_Site(), fake_request)
    # No models registered → no sections returned (not an error).
    assert sections == []
