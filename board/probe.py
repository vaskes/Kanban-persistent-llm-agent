"""
Probe an Agent row's model endpoint and update its reachability state.

The `status` field on Agent is derived from `last_seen_at`. For an agent
whose "presence" *is* answering API calls (i.e. it has no separate worker
daemon), the only honest way to fill that field is to actually call the
model. Doing so here also keeps a stale configuration visible: a row whose
/v1/models returns 401 still flips to reachable the moment the credential
is rotated — and until then, the probe surfaces why.

The HTTP call is made directly (not via OpenAICompatibleProvider.health)
because the provider's health() hides connection errors behind "no models
reachable", and the operator needs to see the real reason.
"""

from __future__ import annotations

from typing import Any

import httpx

from .models import Agent


def _probe_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def probe_agent(agent: Agent) -> dict[str, Any]:
    """
    Hit the agent's configured model endpoint and return what we learned.

    Returns a dict always: callers (admin, CLI, future cron) should never
    have to wrap this in try/except. On success, `last_seen_at` is bumped
    so the existing `status` derivation flips to 'reachable'.
    """
    if not agent.has_model:
        return {"ok": False, "error": "no model configured"}

    url = _probe_url(agent.model_base_url)
    headers = {}
    key = agent.model_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0)) as c:
            r = c.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if r.status_code >= 400:
        return {
            "ok": False,
            "error": f"HTTP {r.status_code} from {url}",
            "status_code": r.status_code,
        }

    try:
        data = r.json().get("data", [])
    except ValueError:
        return {"ok": False, "error": f"non-JSON response from {url}"}

    models = [d.get("id", "") for d in data if d.get("id")]
    if not models:
        return {"ok": False, "error": f"empty model list from {url}"}

    agent.heartbeat()
    return {"ok": True, "models": models, "base_url": agent.model_base_url}

