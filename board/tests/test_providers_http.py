"""
HTTP-level tests for the OpenAI-compatible providers.

These run against a real local HTTP server rather than a mocked httpx client,
so the request shape, status handling and response parsing are all exercised for
real. Mocking httpx would have left the wire format untested — and the wire
format is exactly what breaks when a provider is swapped.

Covers the paths that the FakeProvider tests cannot reach: error statuses,
malformed responses, connection failure, and llama.cpp's per-request `lora`.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from providers.base import (
    CloudProvider,
    LocalLlamaProvider,
    LocalVLLMProvider,
    Message,
    ProviderError,
)


class _Handler(BaseHTTPRequestHandler):
    """Serves whatever the test put in `server.responses`."""

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, body: dict | str, raw: bool = False):
        payload = body if raw else json.dumps(body)
        data = payload.encode() if isinstance(payload, str) else payload
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        mode = self.server.mode
        if self.path.endswith("/models"):
            self._send(200, {"data": [{"id": "test-model"}]})
        else:
            self._send(404, {"error": "no"})

    def do_POST(self):
        self.server.last_request = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
        )
        self.server.last_auth = self.headers.get("Authorization")
        mode = self.server.mode

        if mode == "ok":
            self._send(
                200,
                {
                    "model": "test-model",
                    "choices": [
                        {"message": {"content": "PROVIDER_OK"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
            )
        elif mode == "no_usage":
            self._send(
                200,
                {
                    "model": "test-model",
                    "choices": [{"message": {"content": "hi"}, "finish_reason": "length"}],
                },
            )
        elif mode == "null_content":
            self._send(
                200,
                {
                    "model": "test-model",
                    "choices": [{"message": {"content": None}}],
                    "usage": {},
                },
            )
        elif mode == "malformed":
            self._send(200, {"unexpected": "shape"})
        elif mode == "server_error":
            self._send(500, {"error": "internal"})
        else:  # pragma: no cover
            self._send(404, {"error": "unknown mode"})


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.mode = "ok"
    httpd.last_request = None
    httpd.last_auth = None
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    httpd.base_url = f"http://127.0.0.1:{httpd.server_port}/v1"
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def msgs():
    return [Message("system", "be terse"), Message("user", "say PROVIDER_OK")]


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_completion_over_http(server):
    server.mode = "ok"
    p = LocalVLLMProvider(base_url=server.base_url, api_key="k", default_model="test-model")
    c = p.complete(msgs(), max_tokens=64)

    assert c.text == "PROVIDER_OK"
    assert c.tokens_in == 11
    assert c.tokens_out == 7
    assert c.tokens_total == 18
    assert c.finish_reason == "stop"
    assert c.model == "test-model"
    assert c.latency_ms >= 0


def test_request_carries_messages_and_params(server):
    server.mode = "ok"
    p = LocalVLLMProvider(base_url=server.base_url, api_key="k", default_model="test-model")
    p.complete(msgs(), temperature=0.7, max_tokens=123)

    body = server.last_request
    assert body["model"] == "test-model"
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 123
    assert body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_api_key_is_sent_as_bearer(server):
    server.mode = "ok"
    p = LocalVLLMProvider(base_url=server.base_url, api_key="secret", default_model="m")
    p.complete(msgs())
    assert server.last_auth == "Bearer secret"


def test_no_auth_header_when_key_empty(server):
    server.mode = "ok"
    p = LocalVLLMProvider(base_url=server.base_url, api_key="", default_model="m")
    p.complete(msgs())
    assert server.last_auth is None


def test_list_models(server):
    p = LocalVLLMProvider(base_url=server.base_url, api_key="k")
    assert p.list_models() == ["test-model"]


def test_health_ok(server):
    p = LocalVLLMProvider(base_url=server.base_url, api_key="k")
    h = p.health()
    assert h["ok"] is True
    assert h["models"] == ["test-model"]


def test_missing_usage_defaults_to_zero(server):
    server.mode = "no_usage"
    p = LocalVLLMProvider(base_url=server.base_url, api_key="k", default_model="m")
    c = p.complete(msgs())
    assert c.text == "hi"
    assert c.tokens_in == 0
    assert c.tokens_out == 0
    assert c.finish_reason == "length"


def test_null_content_becomes_empty_string(server):
    server.mode = "null_content"
    p = LocalVLLMProvider(base_url=server.base_url, api_key="k", default_model="m")
    assert p.complete(msgs()).text == ""


# --------------------------------------------------------------------------
# failures
# --------------------------------------------------------------------------


def test_http_500_raises_with_body(server):
    server.mode = "server_error"
    p = LocalVLLMProvider(base_url=server.base_url, api_key="k", default_model="m")
    with pytest.raises(ProviderError, match="HTTP 500"):
        p.complete(msgs())


def test_malformed_response_raises(server):
    server.mode = "malformed"
    p = LocalVLLMProvider(base_url=server.base_url, api_key="k", default_model="m")
    with pytest.raises(ProviderError, match="malformed"):
        p.complete(msgs())


def test_health_reports_false_when_no_models(server):
    server.mode = "server_error"
    p = LocalVLLMProvider(base_url=server.base_url, api_key="k")
    # /models still works in this server, so force a dead base instead
    dead = LocalVLLMProvider(base_url="http://127.0.0.1:59998/v1", api_key="k")
    h = dead.health()
    assert h["ok"] is False
    assert h["provider"] == "local_vllm"
    assert "error" in h


def test_missing_model_raises_before_request(server):
    server.mode = "ok"
    p = LocalVLLMProvider(base_url=server.base_url, api_key="k", default_model="")
    with pytest.raises(ProviderError, match="no model specified"):
        p.complete(msgs())


# --------------------------------------------------------------------------
# llama.cpp specifics
# --------------------------------------------------------------------------


def test_local_llama_defaults_to_first_listed_model(server):
    """With no configured model, the provider adopts whatever the server serves."""
    server.mode = "ok"
    p = LocalLlamaProvider(base_url=server.base_url, api_key="k", default_model="")
    c = p.complete(msgs())
    assert c.text == "PROVIDER_OK"


def test_local_llama_sends_lora_adapters(server):
    """
    The learning-layer hook: per-request adapter mixing at zero context cost.
    The payload must carry llama.cpp's `lora` field verbatim.
    """
    server.mode = "ok"
    p = LocalLlamaProvider(base_url=server.base_url, api_key="k", default_model="m")
    p.complete(
        msgs(),
        lora=[{"id": 0, "scale": 1.0}, {"id": 1, "scale": 0.0}, {"id": 2, "scale": 0.3}],
    )
    lora = server.last_request["lora"]
    assert len(lora) == 3
    assert lora[0] == {"id": 0, "scale": 1.0}
    assert lora[2] == {"id": 2, "scale": 0.3}


def test_local_llama_omits_lora_when_not_requested(server):
    server.mode = "ok"
    p = LocalLlamaProvider(base_url=server.base_url, api_key="k", default_model="m")
    p.complete(msgs())
    assert "lora" not in server.last_request


def test_local_llama_error_paths(server):
    server.mode = "server_error"
    p = LocalLlamaProvider(base_url=server.base_url, api_key="k", default_model="m")
    with pytest.raises(ProviderError, match="local llama HTTP 500"):
        p.complete(msgs())


def test_local_llama_no_models_loaded(server):
    """
    With no configured model and no reachable server, list_models() swallows the
    connection error and returns [], so the failure surfaces as a clear
    "no model loaded" rather than a raw socket exception.
    """
    server.mode = "ok"
    p = LocalLlamaProvider(base_url="http://127.0.0.1:59997/v1", api_key="k", default_model="")
    with pytest.raises(ProviderError, match="no model loaded"):
        p.complete(msgs())


# --------------------------------------------------------------------------
# cloud
# --------------------------------------------------------------------------


def test_cloud_provider_uses_openai_protocol(server):
    server.mode = "ok"
    p = CloudProvider(server.base_url, "key", "test-model", name="minimax")
    assert p.name == "minimax"
    assert p.complete(msgs()).text == "PROVIDER_OK"


def test_cloud_provider_health_names_itself(server):
    p = CloudProvider(server.base_url, "key", "m", name="qwen")
    assert p.health()["provider"] == "qwen"
