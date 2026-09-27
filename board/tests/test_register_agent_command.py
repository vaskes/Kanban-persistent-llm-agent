"""
Tests for the register_agent management command.

The command is the operator-facing entry point for the Agent model. Anything
that can go wrong on the CLI must go wrong loudly with a clear CommandError,
not silently.
"""

import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from board.models import Agent


pytestmark = pytest.mark.django_db


def _invoke(*args, **kwargs):
    out = io.StringIO()
    err = io.StringIO()
    call_command("register_agent", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


def test_registers_a_local_llama_agent():
    out, _ = _invoke(
        "--name", "ornith",
        "--provider", "local_llama",
        "--base-url", "http://192.168.10.7:8080/v1",
        "--model", "Ornith-1.5-35B-A3B-Uncensored",
        "--kind", "worker",
    )
    assert "Registered agent 'ornith'" in out
    a = Agent.objects.get(name="ornith")
    assert a.model_provider == "local_llama"
    assert a.model_base_url == "http://192.168.10.7:8080/v1"
    assert a.model_name == "Ornith-1.5-35B-A3B-Uncensored"


def test_stores_the_api_key_encrypted():
    _invoke(
        "--name", "minimax",
        "--provider", "cloud",
        "--base-url", "https://api.minimax.io/v1",
        "--model", "MiniMax-M3",
        "--api-key", "sk-cp-SECRET-CLI",
    )
    a = Agent.objects.get(name="minimax")
    assert "SECRET-CLI" not in a.model_api_key_cipher  # never plaintext
    assert a.model_api_key() == "sk-cp-SECRET-CLI"


def test_plaintext_key_is_never_written_to_stdout():
    out, _ = _invoke(
        "--name", "quiet",
        "--provider", "cloud",
        "--base-url", "https://example/v1",
        "--model", "m",
        "--api-key", "sk-cp-DO-NOT-LEAK-XYZ",
    )
    assert "DO-NOT-LEAK" not in out


def test_rerun_is_idempotent_and_updates_fields():
    _invoke("--name", "x", "--provider", "local_llama",
            "--base-url", "http://a/v1", "--model", "ma")
    _invoke("--name", "x", "--provider", "local_llama",
            "--base-url", "http://b/v1", "--model", "mb")
    assert Agent.objects.filter(name="x").count() == 1
    a = Agent.objects.get(name="x")
    assert a.model_base_url == "http://b/v1"
    assert a.model_name == "mb"


def test_missing_name_fails():
    with pytest.raises(CommandError):
        _invoke("--provider", "local_llama", "--base-url", "http://x")


def test_missing_provider_fails():
    with pytest.raises(CommandError):
        _invoke("--name", "x", "--base-url", "http://x")


def test_missing_base_url_fails():
    with pytest.raises(CommandError):
        _invoke("--name", "x", "--provider", "local_llama")


def test_invalid_provider_choice_fails():
    with pytest.raises((SystemExit, Exception)):
        _invoke("--name", "x", "--provider", "no-such-thing",
                "--base-url", "http://x")


def test_validation_failure_surfaces_with_a_clear_message():
    from django.core.exceptions import ValidationError
    a = Agent.objects.create(name="broken", model_provider="local_llama")
    try:
        a.full_clean()
    except ValidationError as e:
        assert "model_base_url" in e.message_dict
    else:
        pytest.fail("expected ValidationError")


def test_list_prints_all_agents():
    _invoke("--name", "a", "--provider", "local_llama",
            "--base-url", "http://a/v1", "--model", "ma")
    _invoke("--name", "b", "--provider", "cloud",
            "--base-url", "https://b/v1", "--model", "mb",
            "--api-key", "sk-cp-X")
    out, _ = _invoke("--list")
    assert "a" in out and "b" in out
    assert "local_llama" in out and "cloud" in out
    # list output must not leak the key
    assert "sk-cp-X" not in out


def test_list_with_no_agents_prints_a_friendly_marker():
    out, _ = _invoke("--list")
    assert "no agents registered" in out
