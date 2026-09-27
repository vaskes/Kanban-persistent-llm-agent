"""
Register or update an Agent row with an outbound model endpoint.

Idempotent on `name`: running it twice with the same name updates the row
instead of failing on uniqueness. The plaintext API key never appears in the
output.

Usage:

    .venv/bin/python manage.py register_agent \
        --name Ornith --provider local_llama \
        --base-url http://192.168.10.7:8080/v1 \
        --model "Ornith-1.5-35B-A3B-Uncensored" \
        --kind worker --note "local test llama"

    .venv/bin/python manage.py register_agent \
        --name Minimax --provider cloud \
        --base-url https://api.minimax.io/v1 \
        --model MiniMax-M3 --api-key sk-cp-... \
        --kind assistant

    .venv/bin/python manage.py register_agent --list
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from board.models import Agent


PROVIDERS = ("local_llama", "local_vllm", "cloud")
KINDS = ("worker", "assistant")


class Command(BaseCommand):
    help = "Register or update an Agent with its outbound model configuration."

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
            help="Bearer token; stored encrypted, never logged",
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
            help="print the registered agents and exit",
        )

    def handle(self, *args, **opts):
        if opts["list"]:
            return self._list()

        name = opts["name"]
        if not name:
            raise CommandError("--name is required (or pass --list)")

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
            f"api_key={'set' if agent.model_api_key_cipher else 'unchanged'})"
        ))

    def _list(self) -> None:
        rows = Agent.objects.all().order_by("name")
        if not rows:
            self.stdout.write("(no agents registered)")
            return
        for a in rows:
            self.stdout.write(
                f"{a.name:24s} {a.model_provider:11s} "
                f"{a.model_name:34s} {a.model_base_url} "
                f"reachable={a.is_reachable}"
            )
