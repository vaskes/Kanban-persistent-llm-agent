"""
Tests for the provider abstraction.

The contract matters more than any single implementation: part 2 writes the
runtime against AgentProvider, so the interface must be provably swappable
without touching runtime code.
"""

import pytest

from providers.base import (
    AgentProvider,
    Completion,
    FakeProvider,
    LocalLlamaProvider,
    Message,
    ProviderError,
    build_provider,
)


def test_fake_provider_is_deterministic():
    p = FakeProvider(reply="HELLO")
    msgs = [Message("user", "hi")]
    a = p.complete(msgs)
    b = p.complete(msgs)
    assert a.text == b.text == "HELLO"


def test_fake_provider_records_calls():
    p = FakeProvider()
    p.complete([Message("user", "one")])
    p.complete([Message("user", "two")])
    assert len(p.calls) == 2
    assert p.calls[1][0].content == "two"


def test_fake_provider_health_ok():
    assert FakeProvider().health()["ok"] is True


def test_message_serialises_to_openai_shape():
    assert Message("system", "you are a planner").to_dict() == {
        "role": "system",
        "content": "you are a planner",
    }


def test_completion_totals_tokens():
    c = Completion(text="abcd", tokens_in=10, tokens_out=3)
    assert c.tokens_total == 13


def test_abstract_cannot_be_instantiated():
    with pytest.raises(TypeError):
        AgentProvider()


def test_local_llama_requires_base_url():
    with pytest.raises(ProviderError):
        LocalLlamaProvider(base_url="")


def test_local_llama_unreachable_raises_provider_error():
    p = LocalLlamaProvider(
        base_url="http://127.0.0.1:59999/v1", api_key="x", default_model="m"
    )
    with pytest.raises(ProviderError, match="unreachable"):
        p.complete([Message("user", "hi")])


def test_local_llama_unreachable_health_is_not_ok():
    p = LocalLlamaProvider(
        base_url="http://127.0.0.1:59999/v1", api_key="x", default_model="m"
    )
    h = p.health()
    assert h["ok"] is False
    assert h["provider"] == "local_llama"


@pytest.mark.django_db
def test_build_provider_fake():
    assert isinstance(build_provider("fake"), FakeProvider)


@pytest.mark.django_db
def test_build_provider_unknown_kind_raises():
    with pytest.raises(ProviderError, match="unknown provider"):
        build_provider("does-not-exist")


@pytest.mark.django_db
def test_build_provider_local_llama(settings):
    settings.AGENT_LOCAL_LLAMA_BASE_URL = "http://127.0.0.1:8080/v1"
    p = build_provider("local_llama")
    assert isinstance(p, LocalLlamaProvider)
    assert p.name == "local_llama"


@pytest.mark.django_db
def test_build_provider_minimax_requires_credentials(settings):
    settings.AGENT_MINIMAX_BASE_URL = ""
    settings.AGENT_MINIMAX_API_KEY = ""
    with pytest.raises(ProviderError, match="AGENT_MINIMAX"):
        build_provider("minimax")


@pytest.mark.django_db
def test_build_provider_qwen_requires_credentials(settings):
    settings.AGENT_QWEN_BASE_URL = ""
    settings.AGENT_QWEN_API_KEY = ""
    with pytest.raises(ProviderError, match="AGENT_QWEN"):
        build_provider("qwen")
