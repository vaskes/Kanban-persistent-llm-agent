"""
API-key authentication for agents.

An agent is a headless process. It has no browser, no session cookie and no
human to type a password, so it authenticates with a bearer token in a header.

The credential is looked up by public prefix and compared by SHA-256 hash. Only
the hash is stored — a leaked database row does not yield a usable key.

CSRF is deliberately NOT required for these requests. The standard reasoning
inverts for tokens: a session cookie is sent *ambiently* by the browser, which
is what makes it forgeable by any other site, hence CSRF. A bearer token is
attached explicitly by the calling code and never rides along on a request the
browser would make on its own, so the cross-site attack CSRF defends against
does not apply. See the module docstring on the views for the full argument.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import AgentApiKey

BEARER_PREFIX = "Bearer "
RAW_PREFIX = "Token "
HEADER = "HTTP_AUTHORIZATION"


class InvalidKey(Exception):
    """Raised for a malformed or unknown credential. Never leaks which."""


def extract_secret(request) -> str | None:
    """Pull the secret out of the Authorization header, or None if absent."""
    raw = request.META.get(HEADER, "")
    if not raw:
        return None
    for prefix in (BEARER_PREFIX, RAW_PREFIX):
        if raw.startswith(prefix):
            secret = raw[len(prefix):].strip()
            return secret or None
    return None


def _lookup(raw_token: str) -> tuple[AgentApiKey, str]:
    """
    Split a token into (key row, secret) and verify it.

    Tokens are `kb_<prefix>_<secret>` so the prefix can index a single row
    before any constant-time comparison, and so a leaked log line identifies the
    key without revealing anything usable.
    """
    if not raw_token.startswith("kb_"):
        raise InvalidKey("bad token format")
    parts = raw_token.split("_", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        raise InvalidKey("bad token format")

    _, prefix, secret = parts
    try:
        key = AgentApiKey.objects.select_related("user").get(prefix=prefix)
    except AgentApiKey.DoesNotExist as exc:
        raise InvalidKey("unknown key") from exc

    if not key.is_active:
        raise InvalidKey("key is inactive")
    if not key.check_secret(secret):
        raise InvalidKey("secret mismatch")
    return key, secret


def authenticate_request(request) -> AgentApiKey | None:
    """
    Resolve the bearer token to a key. Returns None when no credential is
    present, so this composes with session auth rather than replacing it.
    """
    raw = extract_secret(request)
    if raw is None:
        return None
    key, _ = _lookup(raw)
    key.mark_used()
    return key


def user_for_key(key: AgentApiKey):
    """The account a key acts as. Permissions are inherited, never widened."""
    user = key.user
    if not user.is_active:
        raise InvalidKey("bound account is inactive")
    return user
