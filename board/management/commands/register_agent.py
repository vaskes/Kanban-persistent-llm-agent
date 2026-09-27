"""
Register or update an Agent row with an outbound model endpoint, and mint an
API key for the agent on first creation.

Idempotent on `name`: re-running updates the row instead of failing on
uniqueness. The API key is auto-minted only on creation — re-running the
command does NOT rotate the key. Pass --rotate-key to mint a fresh one.

The plaintext API key is shown exactly once, in green, on the line right after
the registration line. It is never stored, never logged, and never returned
by --list. Treat the printed line like the SSH key on a freshly-provisioned
box — copy it, hand it to the agent, and never look at it again.

Usage:

    .venv/bin/python manage.py register_agent \\
        --name Ornith --provider local_llama \\
        --base-url http://192.168.10.7:8080/v1 \\
        --model "Ornith-1.5-35B-A3B-Uncensored" \\
        --kind worker --note "local test llama"

    .venv/bin/python manage.py register_agent \\
        --name Minimax --provider cloud \\
        --base-url https://api.minimax.io/v1 \\
        --model MiniMax-M3 --api-key sk-cp-... \\
        --kind assistant

    .venv/bin/python manage.py register_agent --list
    .venv/bin/python manage.py register_agent --rotate-key --name Ornith
"""

from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from board.models import Agent, AgentApiKey


PROVIDERS = ("local_llama", "local_vllm", "cloud")
KINDS = ("worker", "assistant")
PREFIX_BYTES = 6        # → 12 hex chars; visible in logs without leaking
SECRET_BYTES = 32       # → 64 hex chars; the bearer credential


def _generate_key() -> tuple[str, str, str]:
    """
    Returns (full_token, prefix, secret).
    The full token is `<prefix>.<secret>` — the format the API auth layer
    accepts verbatim in Authorization: Bearer <full_token>.
    """
    prefix = secrets.token_hex(PREFIX_BYTES)
    secret = secrets.token_hex(SECRET_BYTES)
    return f"{prefix}.{secret}", prefix, secret


class Command(BaseCommand):
    help = "Register an Agent and mint its API key (admin-only operation)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--name", help="agent display name (unique)")
        parser.add_argument(
            "--provider", choices=PROVIDERS,
            help="which OpenAI-compatible dialect the model speaks",
        )
        parser.add_argument(
            "--base-url", dest="base_url",
            help="where the model lives; /v1 is required for llama.cpp",
        )
        parser.add_argument("--model", help="model id (sent in API calls)")
        parser.add_argument(
            "--api-key", dest="api_key", default="",
            help="model-side Bearer token; stored encrypted, never logged",
        )
        parser.add_argument("--kind", choices=KINDS, default="worker")
        parser.add_argument("--note", default="")
        parser.add_argument(
            "--max-tokens", dest="max_tokens", type=int, default=None,
        )
        parser.add_argument(
            "--temperature", type=float, default=None,
        )
        parser.add_argument(
            "--list", action="store_true",
            help="print registered agents and exit",
        )
        parser.add_argument(
            "--rotate-key", dest="rotate_key", action="store_true",
            help="mint a new API key, replacing any existing one for the agent",
        )
        parser.add_argument(
            "--no-key", dest="no_key", action="store_true",
            help="register the agent but skip auto-minting an API key",
        )

    def handle(self, *args, **opts):
        if opts["list"]:
            return self._list()

        name = opts["name"]
        if not name:
            raise CommandError("--name is required (or pass --list)")

        # --rotate-key operates on an existing agent and is independent of
        # provider/base-url validation — checking those would break the
        # recovery case where the operator needs to rotate credentials on
        # a half-configured row.
        if opts["rotate_key"]:
            return self._rotate(name)

        # Mutual validation: --provider and --base-url travel together, but
        # --model is optional (the provider's /v1/models may discover one).
        for required in ("provider", "base_url"):
            if not opts[required]:
                raise CommandError(f"--{required.replace('_', '-')} is required")

        agent, created = Agent.objects.get_or_create(name=name)
        agent.model_provider = opts["provider"]
        agent.model_base_url = opts["base_url"]
        agent.model_name = opts["model"] or ""
        agent.kind = opts["kind"]
        agent.note = opts["note"]
        agent.model_max_tokens = opts["max_tokens"]
        agent.model_temperature = opts["temperature"]

        # If --api-key was passed (even empty), treat it as authoritative.
        # Otherwise leave the stored key alone.
        if "api_key" in opts and opts["api_key"]:
            agent.set_model_api_key(opts["api_key"])
        agent.full_clean()
        agent.save()

        verb = "Registered" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} agent {agent.name!r} "
            f"(provider={agent.model_provider}, "
            f"base_url={agent.model_base_url}, "
            f"model={agent.model_name or '<discover>'}, "
            f"model_api_key={'set' if agent.model_api_key_cipher else 'unchanged'})"
        ))

        # Auto-mint on first creation. The key is for inbound auth
        # (agent → kanban-web) and is unrelated to the model-side
        # --api-key above (kanban-web → model).
        if created and not opts["no_key"]:
            self._mint_and_print(agent)

    def _rotate(self, name: str) -> None:
        agent = Agent.objects.filter(name=name).first()
        if agent is None:
            raise CommandError(f"no such agent: {name!r}")

        with transaction.atomic():
            # Mark existing keys inactive rather than delete them — the
            # audit trail of "who had access when" is more useful than
            # a clean table.
            AgentApiKey.objects.filter(agent=agent).update(is_active=False)
            self._mint_and_print(agent)

    def _mint_and_print(self, agent: Agent) -> None:
        """Create a fresh WRITE-scope key for the agent, owned by the admin."""
        User = get_user_model()
        # The command runs as the operator who invoked it; we want that user
        # to be the one whose permissions the agent inherits. There is no
        # Django request here, so fall back to the most recent superuser if
        # the caller cannot be determined (e.g. from a cron job).
        admin_user = User.objects.filter(
            is_superuser=True, is_active=True,
        ).order_by("-id").first()
        if admin_user is None:
            raise CommandError(
                "no active superuser to own the agent's API key; "
                "create one before registering agents"
            )

        full, prefix, secret = _generate_key()
        AgentApiKey.objects.create(
            prefix=prefix,
            secret_hash=AgentApiKey.hash_secret(secret),
            label=f"auto-minted for agent {agent.name}",
            scope=AgentApiKey.Scope.WRITE,
            user=admin_user,
            agent=agent,
            is_active=True,
        )

        # The plaintext secret is printed EXACTLY ONCE. Operators are
        # expected to copy it into the agent's environment at this point
        # and never look at it again — there is no way to recover it.
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"  API key for {agent.name!r} (WRITE scope, "
            f"inherits permissions of {admin_user.username!r}):"
        ))
        self.stdout.write(self.style.WARNING(f"    {full}"))
        self.stdout.write(self.style.NOTICE(
            "  ^ shown once, copy it now; cannot be recovered."
        ))
        self.stdout.write("")

    def _list(self) -> None:
        rows = Agent.objects.all().order_by("name")
        if not rows:
            self.stdout.write("(no agents registered)")
            return
        for a in rows:
            key_count = a.api_keys.filter(is_active=True).count()
            self.stdout.write(
                f"{a.name:24s} {a.model_provider:11s} "
                f"{a.model_name:34s} {a.model_base_url} "
                f"reachable={a.is_reachable} "
                f"active_keys={key_count}"
            )
