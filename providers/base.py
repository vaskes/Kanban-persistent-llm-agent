"""
Agent provider abstraction.

Part 1 ships the interface plus three implementations. Nothing calls them yet
from the runtime (that is part 2) — the point is that the contract is fixed and
tested before an executor exists, so the executor cannot invent its own.

Implementations:
  * FakeProvider   — deterministic, for e2e tests
  * LocalLlama     — OpenAI-compatible local server (llama.cpp / vLLM)
  * CloudProvider  — OpenAI-compatible cloud (MiniMax, Qwen)
"""

from __future__ import annotations

import abc
import dataclasses
import time

import httpx


@dataclasses.dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclasses.dataclass
class Completion:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    latency_ms: int = 0
    finish_reason: str = ""

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out


class ProviderError(RuntimeError):
    pass


class AgentProvider(abc.ABC):
    """
    The contract every agent backend must satisfy.

    Deliberately narrow: one method. Anything richer (tool loops, streaming,
    retries) belongs in the runtime, not in the provider, so that swapping
    MiniMax for a local llama.cpp does not change runtime code.
    """

    name: str = "abstract"

    @abc.abstractmethod
    def complete(
        self,
        messages: list[Message],
        *,
        model: str = "",
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> Completion:
        ...

    def health(self) -> dict:
        """Cheap readiness probe. Never raises — returns {'ok': bool, ...}."""
        return {"ok": False, "provider": self.name, "error": "not implemented"}

    def list_models(self) -> list[str]:
        return []


class FakeProvider(AgentProvider):
    """
    Deterministic provider for tests.

    Returns a marker derived from the last user message so assertions can be
    written without depending on model wording.
    """

    name = "fake"

    def __init__(self, reply: str = "FAKE_OK", delay_ms: int = 0) -> None:
        self.reply = reply
        self.delay_ms = delay_ms
        self.calls: list[list[Message]] = []

    def complete(self, messages, *, model="", temperature=0.2, max_tokens=4096):
        self.calls.append(messages)
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000)
        prompt = next(
            (m.content for m in reversed(messages) if m.role == "user"), ""
        )
        return Completion(
            text=self.reply,
            tokens_in=sum(len(m.content) // 4 for m in messages),
            tokens_out=len(self.reply) // 4,
            model="fake",
            finish_reason="stop",
        )

    def health(self) -> dict:
        return {"ok": True, "provider": self.name}


class OpenAICompatibleProvider(AgentProvider):
    """
    Shared implementation for anything speaking the OpenAI chat-completions API.

    Covers LocalLlamaProvider, LocalVLLMProvider and CloudProvider — llama.cpp,
    vLLM, MiniMax and Qwen all speak this dialect, so there is no reason to
    write three nearly identical clients.
    """

    def __init__(self, base_url: str, api_key: str = "", default_model: str = "") -> None:
        if not base_url:
            raise ProviderError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self._client = httpx.Client(timeout=httpx.Timeout(120.0, connect=5.0))

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _model(self, model: str) -> str:
        m = model or self.default_model
        if not m:
            raise ProviderError("no model specified and no default_model configured")
        return m

    def list_models(self) -> list[str]:
        try:
            r = self._client.get(f"{self.base_url}/models", headers=self._headers())
            r.raise_for_status()
            data = r.json().get("data", [])
            return [d.get("id", "") for d in data if d.get("id")]
        except Exception:
            return []

    def health(self) -> dict:
        models = self.list_models()
        if not models:
            return {
                "ok": False,
                "provider": self.name,
                "error": "no models reachable",
            }
        return {
            "ok": True,
            "provider": self.name,
            "base_url": self.base_url,
            "models": models,
        }

    def complete(self, messages, *, model="", temperature=0.2, max_tokens=4096):
        payload = {
            "model": self._model(model),
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        started = time.monotonic()
        try:
            r = self._client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"{self.name} unreachable: {exc}") from exc

        if r.status_code >= 400:
            raise ProviderError(
                f"{self.name} HTTP {r.status_code}: {r.text[:300]}"
            )

        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
            finish = data["choices"][0].get("finish_reason", "")
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"{self.name} malformed response: {r.text[:300]}") from exc

        usage = data.get("usage") or {}
        return Completion(
            text=text,
            tokens_in=int(usage.get("prompt_tokens", 0) or 0),
            tokens_out=int(usage.get("completion_tokens", 0) or 0),
            model=data.get("model", payload["model"]),
            latency_ms=int((time.monotonic() - started) * 1000),
            finish_reason=finish,
        )


class LocalLlamaProvider(OpenAICompatibleProvider):
    """llama.cpp `llama-server`. Works with `--lora` for per-request adapters."""

    name = "local_llama"

    def complete(self, messages, *, model="", temperature=0.2, max_tokens=4096, lora=None):
        """
        `lora` is llama.cpp-specific: list of {"id": int, "scale": float}.

        This is the hook the learning layer needs — a task-kind-specific adapter
        can be switched on per request at zero context cost.
        """
        m = model or self.default_model
        if not m:
            models = self.list_models()
            if not models:
                raise ProviderError("local llama: no model loaded, and none configured")
            m = models[0]
        payload = {
            "model": m,
            "messages": [msg.to_dict() for msg in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if lora:
            payload["lora"] = lora
        started = time.monotonic()
        try:
            r = self._client.post(
                f"{self.base_url}/chat/completions", headers=self._headers(), json=payload
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"local llama unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise ProviderError(f"local llama HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        usage = data.get("usage") or {}
        return Completion(
            text=data["choices"][0]["message"]["content"] or "",
            tokens_in=int(usage.get("prompt_tokens", 0) or 0),
            tokens_out=int(usage.get("completion_tokens", 0) or 0),
            model=data.get("model", m),
            latency_ms=int((time.monotonic() - started) * 1000),
            finish_reason=data["choices"][0].get("finish_reason", ""),
        )


class LocalVLLMProvider(OpenAICompatibleProvider):
    name = "local_vllm"


class CloudProvider(OpenAICompatibleProvider):
    """MiniMax / Qwen and similar. Same protocol, different base_url + key."""

    name = "cloud"

    def __init__(self, base_url: str, api_key: str, default_model: str, name: str) -> None:
        super().__init__(base_url, api_key, default_model)
        self.name = name


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------


def build_provider(kind: str | None = None, **kwargs) -> AgentProvider:
    from django.conf import settings

    kind = kind or settings.AGENT_PROVIDER
    if kind == "fake":
        return FakeProvider()
    if kind in ("local_llama", "llama", "llamacpp"):
        return LocalLlamaProvider(
            base_url=kwargs.get("base_url") or settings.AGENT_LOCAL_LLAMA_BASE_URL,
            api_key=kwargs.get("api_key") or settings.AGENT_LOCAL_LLAMA_API_KEY,
            default_model=kwargs.get("model") or settings.AGENT_LOCAL_LLAMA_MODEL,
        )
    if kind in ("local_vllm", "vllm"):
        return LocalVLLMProvider(
            base_url=kwargs.get("base_url") or settings.AGENT_LOCAL_VLLM_BASE_URL,
            api_key=kwargs.get("api_key", "sk-noop"),
            default_model=kwargs.get("model", ""),
        )
    if kind == "minimax":
        if not settings.AGENT_MINIMAX_BASE_URL or not settings.AGENT_MINIMAX_API_KEY:
            raise ProviderError("minimax: AGENT_MINIMAX_BASE_URL/API_KEY not set")
        return CloudProvider(
            settings.AGENT_MINIMAX_BASE_URL,
            settings.AGENT_MINIMAX_API_KEY,
            kwargs.get("model", ""),
            name="minimax",
        )
    if kind == "qwen":
        if not settings.AGENT_QWEN_BASE_URL or not settings.AGENT_QWEN_API_KEY:
            raise ProviderError("qwen: AGENT_QWEN_BASE_URL/API_KEY not set")
        return CloudProvider(
            settings.AGENT_QWEN_BASE_URL,
            settings.AGENT_QWEN_API_KEY,
            kwargs.get("model", ""),
            name="qwen",
        )
    raise ProviderError(f"unknown provider kind: {kind}")
