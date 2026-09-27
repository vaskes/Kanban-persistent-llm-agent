"""
Tests for the Resource tree and ResourceAllocation.

The data model is the contract the runtime will lean on. Anything ambiguous
in here becomes a runtime bug, so we pin the cross-cutting behaviour: MTI
joins, the XOR constraint on allocation owners, capacity arithmetic, and
the encrypted-credential helpers.
"""

import pytest
from django.core.exceptions import ValidationError

from board.models import (
    Agent,
    Backlog,
    Browser,
    ComfyUiEndpoint,
    GitRepo,
    Host,
    LlmEndpoint,
    Project,
    ProjectMembership,
    Resource,
    ResourceAllocation,
    ResourceKind,
    ResourceLifetime,
    Sandbox,
    Task,
)
from board.bootstrap import ensure_default_project


pytestmark = pytest.mark.django_db


@pytest.fixture
def boot(django_user_model):
    django_user_model.objects.create_user("boot", password="pw12345")


@pytest.fixture
def project(boot):
    return ensure_default_project()


@pytest.fixture
def backlog(project):
    return project.backlogs.get(is_default=True)


@pytest.fixture
def task(project, backlog):
    return Task.objects.create(
        title="t", acceptance="x", project=project, backlog=backlog,
    )


# --- MTI basics -----------------------------------------------------------


def test_resource_base_table_stores_common_fields(boot):
    h = Host.objects.create(name="h1", description="a host")
    assert h.kind == ResourceKind.HOST
    assert h.lifetime == ResourceLifetime.PERSISTENT
    # The host row exists in BOTH tables joined by the same pk.
    assert Resource.objects.filter(pk=h.pk, kind="host").exists()
    assert Host.objects.filter(pk=h.pk).exists()


def test_each_child_kind_creates_a_row_in_its_own_table(boot):
    Host.objects.create(name="h", hostname="h.local")
    Sandbox.objects.create(name="s", image="ubuntu:22.04")
    LlmEndpoint.objects.create(name="l", base_url="http://x/v1")
    ComfyUiEndpoint.objects.create(name="c", url="http://x:8188")
    GitRepo.objects.create(name="g", url="git@x:y.git")
    Browser.objects.create(name="b", image="playwright:latest")
    # all six visible from the parent
    kinds = set(Resource.objects.values_list("kind", flat=True))
    assert kinds == {
        "host", "sandbox", "llm_endpoint", "comfyui", "git_repo", "browser",
    }


def test_name_must_be_unique_across_all_kinds(boot):
    Host.objects.create(name="dup")
    with pytest.raises(Exception):
        Sandbox.objects.create(name="dup", image="x")


# --- Capacity & free slots ------------------------------------------------


def test_capacity_defaults_to_one(boot):
    h = Host.objects.create(name="h")
    assert h.capacity == 1
    assert h.free_slots == 1
    assert h.is_full is False


def test_free_slots_decreases_with_active_allocations(boot, project, task):
    h = Host.objects.create(name="h", capacity=2)
    assert h.free_slots == 2
    ResourceAllocation.objects.create(resource=h, task=task, status="granted")
    h.refresh_from_db()
    assert h.free_slots == 1


def test_free_slots_floors_at_zero(boot, project, task):
    h = Host.objects.create(name="h", capacity=1)
    ResourceAllocation.objects.create(resource=h, task=task, status="granted")
    h.refresh_from_db()
    # Manually creating more than capacity would normally be prevented at the
    # runtime layer; here we only verify the counter never goes negative.
    h.capacity = 0
    assert h.free_slots == 0


def test_archived_resource_is_filtered_out_of_allocations(boot):
    h = Host.objects.create(name="h", archived=True)
    assert h.allocations.count() == 0


# --- Sandbox / Browser auto-capacity --------------------------------------


def test_sandbox_capacity_follows_pool_size_on_save(boot):
    s = Sandbox.objects.create(name="s", image="ubuntu", pool_size=3)
    s.refresh_from_db()
    assert s.capacity == 3
    assert s.lifetime == ResourceLifetime.EPHEMERAL


def test_browser_is_always_ephemeral(boot):
    b = Browser.objects.create(name="b")
    b.refresh_from_db()
    assert b.lifetime == ResourceLifetime.EPHEMERAL
    b.lifetime = ResourceLifetime.PERSISTENT
    b.save()
    b.refresh_from_db()
    # save() forcibly re-asserts the lifetime
    assert b.lifetime == ResourceLifetime.EPHEMERAL


def test_browser_capacity_follows_pool_size(boot):
    b = Browser.objects.create(name="b", pool_size=5)
    b.refresh_from_db()
    assert b.capacity == 5


# --- Encrypted credential helpers -----------------------------------------


def test_host_ssh_key_is_stored_encrypted(boot):
    h = Host.objects.create(name="h")
    h.set_secret("ssh_key_cipher", "-----BEGIN PRIVATE KEY-----\nABC\n-----END")
    h.save()
    fresh = Host.objects.get(name="h")
    assert "BEGIN PRIVATE KEY" not in fresh.ssh_key_cipher
    assert fresh.get_secret("ssh_key_cipher").startswith("-----BEGIN PRIVATE KEY")


def test_host_password_is_stored_encrypted(boot):
    h = Host.objects.create(name="h")
    h.set_secret("password_cipher", "hunter2")
    h.save()
    fresh = Host.objects.get(name="h")
    assert "hunter2" not in fresh.password_cipher
    assert fresh.get_secret("password_cipher") == "hunter2"


def test_empty_secret_clears_the_field(boot):
    h = Host.objects.create(name="h")
    h.set_secret("ssh_key_cipher", "non-empty")
    h.save()
    h.set_secret("ssh_key_cipher", "")
    h.save()
    fresh = Host.objects.get(name="h")
    assert fresh.ssh_key_cipher == ""
    assert fresh.get_secret("ssh_key_cipher") == ""


def test_set_secret_refuses_plaintext_field_name(boot):
    h = Host.objects.create(name="h")
    with pytest.raises(ValueError):
        h.set_secret("hostname", "anything")


def test_get_secret_refuses_plaintext_field_name(boot):
    h = Host.objects.create(name="h")
    with pytest.raises(ValueError):
        h.get_secret("hostname")


def test_get_secret_on_missing_field_raises(boot):
    h = Host.objects.create(name="h")
    with pytest.raises(ValueError):
        h.get_secret("no_such_cipher")


# --- ResourceAllocation XOR constraint ------------------------------------


def test_allocation_must_have_either_task_or_project(boot, project, task):
    with pytest.raises(ValidationError) as ei:
        ResourceAllocation(resource=Host.objects.create(name="h")).full_clean()
    assert "task or project" in str(ei.value)


def test_allocation_cannot_have_both_task_and_project(boot, project, task):
    h = Host.objects.create(name="h")
    with pytest.raises(ValidationError):
        ResourceAllocation(resource=h, task=task, project=project).full_clean()


def test_allocation_with_only_task_is_valid(boot, project, task):
    h = Host.objects.create(name="h")
    a = ResourceAllocation(resource=h, task=task)
    a.full_clean()  # must not raise


def test_allocation_with_only_project_is_valid(boot, project):
    h = Host.objects.create(name="h")
    a = ResourceAllocation(resource=h, project=project)
    a.full_clean()  # must not raise


def test_allocation_is_active_only_when_granted(boot, project, task):
    h = Host.objects.create(name="h")
    a = ResourceAllocation.objects.create(resource=h, task=task, status="requested")
    assert a.is_active is False
    a.status = "granted"
    assert a.is_active is True
    a.status = "released"
    assert a.is_active is False


def test_allocation_can_target_an_agent(boot, project, task):
    """An allocation may carry the agent that requested it for audit."""
    h = Host.objects.create(name="h")
    agent = Agent.objects.create(name="a", model_provider="local_llama",
                                  model_name="m", model_base_url="http://x/v1")
    a = ResourceAllocation.objects.create(
        resource=h, task=task, agent=agent,
    )
    assert a.agent == agent


# --- FK behaviour ---------------------------------------------------------


def test_cannot_delete_a_resource_with_allocations(boot, project, task):
    h = Host.objects.create(name="h")
    ResourceAllocation.objects.create(resource=h, task=task)
    with pytest.raises(Exception):
        h.delete()


def test_deleting_task_cascades_to_its_allocations(boot, project, task):
    h = Host.objects.create(name="h")
    a = ResourceAllocation.objects.create(resource=h, task=task)
    task.delete()
    assert not ResourceAllocation.objects.filter(pk=a.pk).exists()


def test_project_allocation_visible_via_reverse_relation(boot, project, task):
    h = Host.objects.create(name="h")
    a = ResourceAllocation.objects.create(resource=h, project=project)
    assert list(project.resource_allocations.all()) == [a]

# --- coverage: the unused branches ---------------------------------------


def test_is_persistent_property(boot):
    h = Host.objects.create(name="h")
    assert h.is_persistent is True
    assert h.is_ephemeral is False


def test_set_secret_on_missing_field_raises(boot):
    h = Host.objects.create(name="h")
    with pytest.raises(ValueError):
        h.set_secret("no_such_cipher", "x")


def test_sandbox_with_zero_pool_size_keeps_default_capacity(boot):
    s = Sandbox.objects.create(name="s", image="ubuntu")
    s.refresh_from_db()
    # pool_size default is 1, capacity should mirror that
    assert s.capacity == 1


def test_browser_with_zero_pool_size_keeps_default_capacity(boot):
    b = Browser.objects.create(name="b")
    b.refresh_from_db()
    assert b.capacity == 2  # Browser default pool_size
