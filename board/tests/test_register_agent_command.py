"""
Tests for the register_agent management command.

The command is the operator-facing entry point for the Agent model. Anything
that can go wrong on the CLI must go wrong loudly with a clear CommandError,
not silently. The auto-mint path is also covered here: registering an agent
mints an API key for it and shows the plaintext exactly once.
"""

import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from board.models import Agent, AgentApiKey


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def admin(django_user_model):
    """Auto-mint needs an active superuser to own the minted key."""
    django_user_model.objects.create_user(
        "boss", password="pw", is_staff=True, is_superuser=True,
    )


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
        "--model", "Ornith-1.5-35B-Uncensored",
        "--kind", "worker",
    )
    assert "Registered agent 'ornith'" in out
    a = Agent.objects.get(name="ornith")
    assert a.model_provider == "local_llama"
    assert a.model_base_url == "http://192.168.10.7:8080/v1"
    assert a.model_name == "Ornith-1.5-35B-Uncensored"


def test_stores_the_model_api_key_encrypted():
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


def test_plaintext_model_key_is_never_written_to_stdout():
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
    # list output must not leak the model-side key
    assert "sk-cp-X" not in out
    # and the auto-minted api-key count is shown per row
    assert "active_keys=1" in out


def test_list_with_no_agents_prints_a_friendly_marker():
    out, _ = _invoke("--list")
    assert "no agents registered" in out


# ---------------------------------------------------------------------------
# Auto-mint of an API key on agent registration
# ---------------------------------------------------------------------------


def test_first_registration_auto_mints_one_active_write_key():
    out, _ = _invoke(
        "--name", "fresh",
        "--provider", "local_llama",
        "--base-url", "http://x/v1",
        "--model", "m",
    )
    a = Agent.objects.get(name="fresh")
    keys = list(a.api_keys.all())
    assert len(keys) == 1
    k = keys[0]
    assert k.is_active is True
    assert k.scope == AgentApiKey.Scope.WRITE
    assert k.agent_id == a.pk
    assert k.user is not None
    assert k.user.is_superuser  # auto-mint picks an admin
    # and the plaintext token is in the output, exactly once, with the
    # recovery warning alongside it
    assert "." in out
    assert "shown once" in out or "copy it now" in out


def test_rerun_does_not_rotate_the_key():
    """Re-running the command updates the agent row but leaves the key alone.
    A rotation is a deliberate operator action, not a side effect of a typo."""
    _invoke("--name", "x", "--provider", "local_llama",
            "--base-url", "http://a/v1", "--model", "ma")
    _invoke("--name", "x", "--provider", "local_llama",
            "--base-url", "http://b/v1", "--model", "mb")
    a = Agent.objects.get(name="x")
    assert a.api_keys.count() == 1


def test_rotate_key_mints_a_new_one_and_disables_the_old():
    _invoke("--name", "r", "--provider", "local_llama",
            "--base-url", "http://a/v1", "--model", "m")
    a = Agent.objects.get(name="r")
    old = a.api_keys.get()
    assert old.is_active

    _invoke("--rotate-key", "--name", "r")
    a.refresh_from_db()
    assert a.api_keys.count() == 2
    old.refresh_from_db()
    assert old.is_active is False
    new = a.api_keys.filter(is_active=True).get()
    assert new.pk != old.pk
    assert new.agent_id == a.pk


def test_no_key_flag_skips_auto_minting():
    _invoke("--name", "n", "--provider", "local_llama",
            "--base-url", "http://a/v1", "--model", "m", "--no-key")
    a = Agent.objects.get(name="n")
    assert a.api_keys.count() == 0


def test_auto_mint_fails_clearly_when_no_admin_exists(django_user_model):
    """Without an active superuser, minting a key must raise CommandError,
    not silently pick a non-admin or leave the agent keyless."""
    # The autouse `admin` fixture already created one; we delete it here.
    django_user_model.objects.filter(is_superuser=True).delete()
    with pytest.raises(CommandError) as ei:
        _invoke(
            "--name", "lonely",
            "--provider", "local_llama",
            "--base-url", "http://x/v1",
            "--model", "m",
        )
    assert "no active superuser" in str(ei.value)
    # the agent itself was still created — only the key mint failed
    assert Agent.objects.filter(name="lonely").exists()
    assert Agent.objects.get(name="lonely").api_keys.count() == 0


def test_minted_key_authenticates_against_check_secret():
    """The plaintext printed once must actually unlock the bearer endpoint."""
    out, _ = _invoke(
        "--name", "authn",
        "--provider", "local_llama",
        "--base-url", "http://x/v1",
        "--model", "m",
    )
    # Parse the token line — format: "    <prefix>.<secret>"
    line = [l for l in out.splitlines() if "." in l and len(l.split(".")[1]) > 10][0]
    token = line.strip()
    prefix, secret = token.split(".", 1)
    k = AgentApiKey.objects.get(prefix=prefix)
    assert k.check_secret(secret) is True
    # any other secret must not unlock it
    assert k.check_secret(secret + "x") is False


def test_rotate_key_with_unknown_agent_raises(django_user_model):
    with pytest.raises(CommandError):
        _invoke("--rotate-key", "--name", "ghost")


def test_rotate_key_does_not_require_provider_or_base_url(django_user_model):
    """Rotation is a credential operation; it should work on a half-configured row."""
    Agent.objects.create(name="bare")
    _invoke("--rotate-key", "--name", "bare")
    assert Agent.objects.get(name="bare").api_keys.filter(is_active=True).count() == 1
