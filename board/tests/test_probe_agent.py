"""
Tests for board.probe.probe_agent and the probe_agent management command.

The probe is the operator's only way to tell "registered" apart from
"actually answering", so its semantics must not drift. We never make real
HTTP calls here — httpx is replaced with a stub so the tests run offline
and assert what the probe does with every response shape.
"""

import io

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from board.management.commands.probe_agent import Command as ProbeCmd
from board.models import Agent
from board.probe import probe_agent


pytestmark = pytest.mark.django_db


@pytest.fixture
def boot(django_user_model):
    django_user_model.objects.create_user("boot", password="pw12345")


def _stub_client_factory(payloads):
    """Return an httpx.Client subclass that returns the queued responses."""
    queue = list(payloads)

    class FakeClient:
        def __init__(self, *a, **kw):
            pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            r = httpx.Response(
                item["status"], json=item.get("body"),
                request=httpx.Request("GET", url),
            )
            return r
        def post(self, *a, **kw):
            raise AssertionError("probe should not POST")

    return FakeClient


@pytest.fixture
def local_llama_agent(boot):
    return Agent.objects.create(
        name="ornith",
        model_provider="local_llama",
        model_name="Ornith-1.5-35B-A3B-Uncensored",
        model_base_url="http://192.168.10.7:8080/v1",
    )


@pytest.fixture
def cloud_agent(boot):
    a = Agent.objects.create(
        name="minimax",
        model_provider="cloud",
        model_name="MiniMax-M3",
        model_base_url="https://api.minimax.io/v1",
    )
    a.set_model_api_key("sk-cp-test")
    a.save()
    return a


# --- probe_agent ----------------------------------------------------------


def test_probe_returns_not_applicable_for_unconfigured_agent(boot):
    a = Agent.objects.create(name="naked")
    assert probe_agent(a) == {"ok": False, "error": "no model configured"}


def test_probe_bumps_last_seen_on_success(boot, monkeypatch, local_llama_agent):
    monkeypatch.setattr(
        "board.probe.httpx.Client",
        _stub_client_factory([{"status": 200, "body": {"data": [
            {"id": "ornith-1.5"}, {"id": "ornith-2"}, {"id": "ornith-3"},
        ]}}]),
    )
    assert local_llama_agent.last_seen_at is None
    assert local_llama_agent.model_checked_at is None
    r = probe_agent(local_llama_agent)
    assert r["ok"] is True
    assert r["models"] == ["ornith-1.5", "ornith-2", "ornith-3"]
    local_llama_agent.refresh_from_db()
    assert local_llama_agent.last_seen_at is not None
    assert local_llama_agent.model_checked_at is not None
    assert local_llama_agent.model_check_ok is True
    assert local_llama_agent.model_check_error == ""


def test_probe_reports_failure_without_bumping_last_seen(
    boot, monkeypatch, local_llama_agent,
):
    monkeypatch.setattr(
        "board.probe.httpx.Client",
        _stub_client_factory([{"status": 503, "body": {"error": "down"}}]),
    )
    r = probe_agent(local_llama_agent)
    assert r["ok"] is False
    local_llama_agent.refresh_from_db()
    assert local_llama_agent.last_seen_at is None
    # but the failure IS recorded so the admin can show why
    assert local_llama_agent.model_checked_at is not None
    assert local_llama_agent.model_check_ok is False
    assert "503" in local_llama_agent.model_check_error


def test_probe_handles_connection_refused(boot, monkeypatch, local_llama_agent):
    monkeypatch.setattr(
        "board.probe.httpx.Client",
        _stub_client_factory([httpx.ConnectError("refused")]),
    )
    r = probe_agent(local_llama_agent)
    assert r["ok"] is False
    assert "ConnectError" in r["error"]


def test_probe_handles_empty_models_list_as_failure(
    boot, monkeypatch, local_llama_agent,
):
    monkeypatch.setattr(
        "board.probe.httpx.Client",
        _stub_client_factory([{"status": 200, "body": {"data": []}}]),
    )
    r = probe_agent(local_llama_agent)
    assert r["ok"] is False
    assert "empty model list" in r["error"]


# --- probe_agent command --------------------------------------------------


def test_command_probes_all_agents_and_exits_nonzero_on_any_failure(
    boot, monkeypatch, local_llama_agent, cloud_agent,
):
    # local ok, cloud 401 → mixed → exit 1
    monkeypatch.setattr(
        "board.probe.httpx.Client",
        _stub_client_factory([
            {"status": 200, "body": {"data": [{"id": "ornith-1"}]}},
            {"status": 401, "body": {"error": "bad key"}},
        ]),
    )
    out, err = io.StringIO(), io.StringIO()
    with pytest.raises(CommandError):
        call_command("probe_agent", stdout=out, stderr=err)
    s = out.getvalue() + err.getvalue()
    assert "ornith" in s
    assert "OK" in s
    assert "minimax" in s
    assert "FAIL" in s


def test_command_exits_zero_when_all_pass(
    boot, monkeypatch, local_llama_agent, cloud_agent,
):
    monkeypatch.setattr(
        "board.probe.httpx.Client",
        _stub_client_factory([
            {"status": 200, "body": {"data": [{"id": "o"}]}},
            {"status": 200, "body": {"data": [{"id": "m"}]}},
        ]),
    )
    out = io.StringIO()
    call_command("probe_agent", stdout=out)
    assert "OK" in out.getvalue()


def test_command_with_specific_name_probes_only_that_agent(
    boot, monkeypatch, local_llama_agent, cloud_agent,
):
    monkeypatch.setattr(
        "board.probe.httpx.Client",
        _stub_client_factory([{"status": 200, "body": {"data": [{"id": "o"}]}}]),
    )
    out = io.StringIO()
    call_command("probe_agent", "--name", "ornith", stdout=out)
    assert "ornith" in out.getvalue()
    # cloud's probe must not have been called → no second stub queued
    assert "minimax" not in out.getvalue()


def test_command_with_unknown_name_raises_commanderror(
    boot, monkeypatch, local_llama_agent,
):
    monkeypatch.setattr("board.probe.httpx.Client", _stub_client_factory([]))
    with pytest.raises(CommandError) as ei:
        call_command("probe_agent", "--name", "ghost")
    assert "no such agent" in str(ei.value)
    assert "ghost" in str(ei.value)


def test_command_with_no_agents_prints_a_marker_and_returns_ok(boot):
    out = io.StringIO()
    call_command("probe_agent", stdout=out)
    assert "no agents registered" in out.getvalue()


# --- URL handling edge cases ---------------------------------------------


def test_probe_appends_v1_when_base_url_lacks_it(boot, monkeypatch):
    """If the operator forgot /v1, the probe must add it instead of 404'ing."""
    a = Agent.objects.create(
        name="forgot",
        model_provider="local_llama",
        model_name="m",
        model_base_url="http://localhost:8080",  # no /v1
    )
    captured = {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            captured["url"] = url
            return httpx.Response(
                200, json={"data": [{"id": "m"}]},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr("board.probe.httpx.Client", FakeClient)
    r = probe_agent(a)
    assert r["ok"] is True
    assert captured["url"].endswith("/v1/models")


def test_probe_does_not_double_append_v1(boot, monkeypatch):
    a = Agent.objects.create(
        name="ok",
        model_provider="local_llama",
        model_name="m",
        model_base_url="http://localhost:8080/v1",
    )
    captured = {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            captured["url"] = url
            return httpx.Response(
                200, json={"data": [{"id": "m"}]},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr("board.probe.httpx.Client", FakeClient)
    probe_agent(a)
    # exactly one /v1/models, never /v1/v1/models
    assert captured["url"].count("/v1/models") == 1


def test_probe_sends_authorization_header_when_key_is_set(
    boot, monkeypatch,
):
    a = Agent.objects.create(
        name="cloud",
        model_provider="cloud",
        model_name="m",
        model_base_url="https://x/v1",
    )
    a.set_model_api_key("sk-cp-test")
    a.save()

    captured = {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            captured["headers"] = headers or {}
            return httpx.Response(
                200, json={"data": [{"id": "m"}]},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr("board.probe.httpx.Client", FakeClient)
    probe_agent(a)
    assert captured["headers"].get("Authorization") == "Bearer sk-cp-test"


def test_probe_handles_non_json_response(boot, monkeypatch):
    a = Agent.objects.create(
        name="html",
        model_provider="local_llama",
        model_name="m",
        model_base_url="http://x/v1",
    )

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            return httpx.Response(
                200, content=b"<html>nope</html>",
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr("board.probe.httpx.Client", FakeClient)
    r = probe_agent(a)
    assert r["ok"] is False
    assert "non-JSON" in r["error"]


def test_probe_reports_http_error_codes(boot, monkeypatch):
    a = Agent.objects.create(
        name="denied",
        model_provider="cloud",
        model_name="m",
        model_base_url="https://x/v1",
    )
    a.set_model_api_key("sk-cp-bad")
    a.save()

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None):
            return httpx.Response(
                401, json={"error": "bad key"},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr("board.probe.httpx.Client", FakeClient)
    r = probe_agent(a)
    assert r["ok"] is False
    assert "401" in r["error"]
