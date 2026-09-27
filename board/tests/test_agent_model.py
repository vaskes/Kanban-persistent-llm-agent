"""
Tests for the model-endpoint extension on Agent.

The Agent row now stores enough information to call an OpenAI-compatible
provider directly. These tests pin the contract: encryption round-trip,
provider factory, validation, and the property helpers the UI relies on.
"""

import pytest

from board.models import Agent
from providers.base import (
    CloudProvider, LocalLlamaProvider, ProviderError,
)


pytestmark = pytest.mark.django_db


@pytest.fixture
def boot_admin(django_user_model):
    """Seed the first user so subsequent users are NOT auto-promoted."""
    django_user_model.objects.create_user("boot", password="pw12345")


def _make(name, **kw):
    return Agent.objects.create(name=name, **kw)


# --- encryption round-trip ------------------------------------------------


def test_model_api_key_is_stored_encrypted(boot_admin):
    a = _make("alice")
    a.set_model_api_key("sk-cp-SECRET-123")
    a.save()
    # reload from DB
    fresh = Agent.objects.get(name="alice")
    # plaintext must NOT appear in the DB
    assert "SECRET-123" not in fresh.model_api_key_cipher
    assert fresh.model_api_key() == "sk-cp-SECRET-123"


def test_empty_key_round_trips_to_empty(boot_admin):
    a = _make("bob")
    assert a.model_api_key() == ""
    a.set_model_api_key("")
    a.save()
    assert Agent.objects.get(name="bob").model_api_key() == ""


def test_setting_an_empty_key_clears_a_previous_one(boot_admin):
    a = _make("carol")
    a.set_model_api_key("sk-cp-OLD")
    a.save()
    a.set_model_api_key("")
    a.save()
    assert Agent.objects.get(name="carol").model_api_key() == ""


# --- provider factory -----------------------------------------------------


def test_local_llama_provider_is_built_correctly(boot_admin):
    a = _make("ornith",
              model_provider="local_llama",
              model_name="Ornith-1.5-35B-A3B-Uncensored",
              model_base_url="http://192.168.10.7:8080/v1")
    p = a.provider()
    assert isinstance(p, LocalLlamaProvider)
    assert p.base_url == "http://192.168.10.7:8080/v1"
    assert p.default_model == "Ornith-1.5-35B-A3B-Uncensored"


def test_local_vllm_provider_is_built_correctly(boot_admin):
    a = _make("vllm",
              model_provider="local_vllm",
              model_name="some-model",
              model_base_url="http://localhost:8000/v1")
    from providers.base import LocalVLLMProvider
    p = a.provider()
    assert isinstance(p, LocalVLLMProvider)


def test_unknown_provider_value_raises(boot_admin):
    a = _make("weird")
    a.model_provider = "no_such_provider"
    a.model_base_url = "http://x/v1"
    with pytest.raises(ProviderError):
        a.provider()


def test_cloud_provider_is_built_with_an_api_key(boot_admin):
    a = _make("minimax",
              model_provider="cloud",
              model_name="MiniMax-M3",
              model_base_url="https://api.minimax.io/v1")
    a.set_model_api_key("sk-cp-DECRYPTED")
    p = a.provider()
    assert isinstance(p, CloudProvider)
    assert p.api_key == "sk-cp-DECRYPTED"
    assert p.default_model == "MiniMax-M3"


def test_provider_fails_when_no_model_is_configured(boot_admin):
    a = _make("naked")
    with pytest.raises(ProviderError):
        a.provider()


def test_has_model_requires_both_provider_and_base_url(boot_admin):
    a = _make("half")
    assert a.has_model is False
    a.model_provider = "local_llama"
    a.save()
    assert a.has_model is False  # base_url still empty
    a.model_base_url = "http://localhost:8080/v1"
    a.save()
    assert a.has_model is True


# --- validation -----------------------------------------------------------


def test_provider_without_base_url_fails_clean(boot_admin):
    a = Agent(model_provider="local_llama", name="bad")
    from django.core.exceptions import ValidationError
    with pytest.raises(ValidationError) as ei:
        a.clean()
    assert "model_base_url" in ei.value.message_dict


def test_base_url_without_provider_fails_clean(boot_admin):
    a = Agent(model_base_url="http://localhost:8080/v1", name="bad2")
    from django.core.exceptions import ValidationError
    with pytest.raises(ValidationError) as ei:
        a.clean()
    assert "model_provider" in ei.value.message_dict


def test_fully_blank_passes_clean(boot_admin):
    """An agent with no model fields is allowed — it just can't be dispatched to."""
    a = Agent(name="plain")
    a.clean()  # must not raise


# --- uniqueness ------------------------------------------------------------


def test_agent_names_must_be_unique(boot_admin):
    _make("dup")
    with pytest.raises(Exception):
        _make("dup")


# --- status (unchanged behaviour) -----------------------------------------


def test_status_is_unreachable_when_never_seen(boot_admin):
    a = _make("ghost")
    assert a.status == Agent.Status.UNREACHABLE
    assert a.is_reachable is False


def test_status_becomes_reachable_after_heartbeat(boot_admin):
    a = _make("alive")
    a.heartbeat()
    a.refresh_from_db()
    assert a.is_reachable is True
