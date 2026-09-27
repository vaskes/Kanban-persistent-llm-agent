"""
Coverage for the remaining branches: provider defaults, factory wiring, and
the small conditional paths in the state machine.

Kept in one file deliberately — these are the edges, and they belong together.
"""

import pytest

from board.models import Actor, Status, Task
from board.state import Ctx, TransitionError, claim, heartbeat, transition
from providers.base import (
    AgentProvider,
    Completion,
    FakeProvider,
    LocalLlamaProvider,
    LocalVLLMProvider,
    Message,
    build_provider,
)

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# provider base-class defaults
# --------------------------------------------------------------------------


class _Bare(AgentProvider):
    """A provider that overrides only the required method."""

    name = "bare"

    def complete(self, messages, *, model="", temperature=0.2, max_tokens=4096):
        return Completion(text="", model="bare")


def test_base_health_default_is_not_ok():
    h = _Bare().health()
    assert h == {"ok": False, "provider": "bare", "error": "not implemented"}


def test_base_list_models_default_is_empty():
    assert _Bare().list_models() == []


def test_fake_provider_honours_delay():
    import time

    p = FakeProvider(reply="slow", delay_ms=20)
    t0 = time.monotonic()
    p.complete([Message("user", "x")])
    assert time.monotonic() - t0 >= 0.015


def test_openai_compatible_unreachable_raises_named_error():
    """LocalVLLM inherits the shared unreachable path, distinct from llama's."""
    p = LocalVLLMProvider(
        base_url="http://127.0.0.1:59996/v1", api_key="k", default_model="m"
    )
    with pytest.raises(Exception) as exc:
        p.complete([Message("user", "hi")])
    assert "local_vllm" in str(exc.value)
    assert "unreachable" in str(exc.value)


# --------------------------------------------------------------------------
# factory wiring — every documented kind must actually construct
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_factory_builds_local_vllm(settings):
    settings.AGENT_LOCAL_VLLM_BASE_URL = "http://127.0.0.1:8000/v1"
    p = build_provider("local_vllm")
    assert isinstance(p, LocalVLLMProvider)
    assert p.name == "local_vllm"


@pytest.mark.django_db
def test_factory_builds_minimax(settings):
    settings.AGENT_MINIMAX_BASE_URL = "https://api.example.invalid/v1"
    settings.AGENT_MINIMAX_API_KEY = "key"
    p = build_provider("minimax", model="minimax-model")
    assert p.name == "minimax"
    assert p.default_model == "minimax-model"


@pytest.mark.django_db
def test_factory_builds_qwen(settings):
    settings.AGENT_QWEN_BASE_URL = "https://dashscope.example.invalid/v1"
    settings.AGENT_QWEN_API_KEY = "key"
    p = build_provider("qwen", model="qwen-model")
    assert p.name == "qwen"


@pytest.mark.django_db
def test_factory_accepts_aliases(settings):
    settings.AGENT_LOCAL_LLAMA_BASE_URL = "http://127.0.0.1:8080/v1"
    for alias in ("llama", "llamacpp", "local_llama"):
        assert isinstance(build_provider(alias), LocalLlamaProvider)
    settings.AGENT_LOCAL_VLLM_BASE_URL = "http://127.0.0.1:8000/v1"
    assert isinstance(build_provider("vllm"), LocalVLLMProvider)


@pytest.mark.django_db
def test_factory_falls_back_to_settings_kind(settings):
    settings.AGENT_PROVIDER = "fake"
    assert isinstance(build_provider(None), FakeProvider)


@pytest.mark.django_db
def test_factory_kwargs_override_settings(settings):
    settings.AGENT_LOCAL_LLAMA_BASE_URL = "http://from-settings.invalid/v1"
    p = build_provider("local_llama", base_url="http://from-kwargs.invalid/v1")
    assert p.base_url == "http://from-kwargs.invalid/v1"


# --------------------------------------------------------------------------
# Ctx defaults
# --------------------------------------------------------------------------


def test_ctx_extra_defaults_to_empty_dict():
    assert Ctx().extra == {}


def test_ctx_extra_preserved_when_given():
    assert Ctx(extra={"k": 1}).extra == {"k": 1}


def test_ctx_defaults():
    c = Ctx()
    assert c.actor == Actor.SYSTEM
    assert c.reason == ""
    assert c.evidence is None
    assert c.allow_budget_override is False


# --------------------------------------------------------------------------
# heartbeat without a step
# --------------------------------------------------------------------------


def test_heartbeat_without_step_only_touches_liveness():
    t = Task.objects.create(
        title="h", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    got = claim("w1", lease_seconds=900)
    got.current_step = "previous step"
    Task.objects.filter(pk=got.pk).update(current_step="previous step")

    heartbeat(got)
    got.refresh_from_db()
    assert got.current_step == "previous step"
    assert got.heartbeat_at is not None


def test_heartbeat_updates_in_memory_too():
    t = Task.objects.create(
        title="h", acceptance="x", status=Status.READY, autonomy="AUTO"
    )
    got = claim("w1", lease_seconds=900)
    heartbeat(got, "now doing X")
    assert got.current_step == "now doing X"  # instance updated without refresh


# --------------------------------------------------------------------------
# operator-only guard: both directions
# --------------------------------------------------------------------------


def test_operator_only_guard_allows_operator():
    t = Task.objects.create(
        title="op", acceptance="x", status=Status.BACKLOG
    )
    transition(t, Status.CANCELLED, Ctx(actor=Actor.OPERATOR))
    assert t.status == Status.CANCELLED


def test_operator_only_guard_blocks_agent_with_named_reason():
    t = Task.objects.create(
        title="op", acceptance="x", status=Status.BACKLOG
    )
    with pytest.raises(TransitionError, match="agent may not"):
        transition(t, Status.CANCELLED, Ctx(actor=Actor.AGENT))


# --------------------------------------------------------------------------
# budget override flag
# --------------------------------------------------------------------------


def test_allow_budget_override_flag():
    t = Task.objects.create(
        title="b",
        acceptance="x",
        status=Status.READY,
        autonomy="AUTO",
        attempts=9,
        max_attempts=1,
    )
    with pytest.raises(TransitionError, match="attempts exhausted"):
        transition(t, Status.IN_PROGRESS, Ctx(actor=Actor.SYSTEM))
    transition(
        t, Status.IN_PROGRESS, Ctx(actor=Actor.SYSTEM, allow_budget_override=True)
    )
    assert t.status == Status.IN_PROGRESS
