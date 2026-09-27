"""
The 'Probe reachability' admin action is the UI path to what the
manage.py probe_agent CLI does; the operator may not have shell access on
the box, so the admin button must surface the same information.
"""

import pytest
from django.contrib import admin as dj_admin
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from board.models import Agent


pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(django_user_model):
    return django_user_model.objects.create_user(
        "staff", password="pw12345", is_staff=True, is_superuser=True,
    )


@pytest.fixture
def staff_client(staff):
    c = Client()
    c.force_login(staff)
    return c


@pytest.fixture
def an_agent(boot_or_staff):
    a = Agent.objects.create(
        name="victim",
        model_provider="local_llama",
        model_name="m",
        model_base_url="http://localhost:9999/v1",
    )
    return a


@pytest.fixture
def boot_or_staff(django_user_model):
    django_user_model.objects.create_user("boot", password="x")


def test_probe_action_is_registered_on_agent_admin():
    """The action must show up on the Agent changelist, not be a hidden method."""
    from board.admin import AgentAdmin
    assert "probe_reachability" in AgentAdmin.actions


def test_probe_action_calls_probe_for_every_selected_row(
    staff_client, an_agent, monkeypatch,
):
    seen = []
    def fake_probe(agent):
        seen.append(agent.name)
        return {"ok": True, "models": ["x"], "base_url": "http://x"}
    monkeypatch.setattr("board.probe.probe_agent", fake_probe)

    url = reverse("admin:board_agent_changelist")
    r = staff_client.post(url, {
        "action": "probe_reachability",
        "_selected_action": [str(an_agent.pk)],
    }, follow=True)
    assert r.status_code == 200
    assert seen == ["victim"]


def test_probe_action_surfaces_ok_and_fail_in_messages(
    staff_client, an_agent, monkeypatch,
):
    def fake_probe(agent):
        return {"ok": True, "models": ["alpha", "beta"], "base_url": "x"}
    monkeypatch.setattr("board.probe.probe_agent", fake_probe)
    url = reverse("admin:board_agent_changelist")
    r = staff_client.post(url, {
        "action": "probe_reachability",
        "_selected_action": [str(an_agent.pk)],
    }, follow=True)
    msgs = [str(m) for m in r.context["messages"]]
    assert any("victim: OK" in m for m in msgs), msgs


def test_probe_action_surfaces_failure_in_messages(
    staff_client, an_agent, monkeypatch,
):
    def fake_probe(agent):
        return {"ok": False, "error": "ConnectError: refused"}
    monkeypatch.setattr("board.probe.probe_agent", fake_probe)
    url = reverse("admin:board_agent_changelist")
    r = staff_client.post(url, {
        "action": "probe_reachability",
        "_selected_action": [str(an_agent.pk)],
    }, follow=True)
    msgs = [str(m) for m in r.context["messages"]]
    joined = " | ".join(msgs)
    assert "victim: FAIL" in joined, joined
    assert "ConnectError" in joined, joined
