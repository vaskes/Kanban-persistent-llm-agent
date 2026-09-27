"""
Mint an API key for an agent or script.

    python manage.py create_api_key --user worker --label "dreamline regen"
    python manage.py create_api_key --user worker --scope read --label "dashboard"

The plaintext token is printed once and never stored. Only its SHA-256 hash
goes into the database, so a leaked table does not yield usable credentials.
If it is lost, mint a new one; there is nothing to recover.
"""

from __future__ import annotations

import hashlib
import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from board.models import AgentApiKey


def mint(prefix: str | None = None) -> tuple[str, str, str]:
    """Return (token, prefix, secret_hash). The token is shown exactly once."""
    pfx = prefix or secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    token = f"kb_{pfx}_{secret}"
    return token, pfx, hashlib.sha256(secret.encode()).hexdigest()


class Command(BaseCommand):
    help = "Create an API key for an agent or script."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="account the key acts as")
        parser.add_argument("--label", default="", help="human note, e.g. its purpose")
        parser.add_argument(
            "--scope",
            choices=[s for s, _ in AgentApiKey.Scope.choices],
            default=AgentApiKey.Scope.WRITE,
            help="write = may claim/heartbeat/review; read = list only",
        )
        parser.add_argument(
            "--prefix",
            default=None,
            help="custom public prefix, for example the worker's name",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        User = get_user_model()
        username = opts["user"]
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"no such user: {username!r}")
        if not user.is_active:
            raise CommandError(f"user {username!r} is inactive")

        prefix = opts["prefix"]
        if prefix and AgentApiKey.objects.filter(prefix=prefix).exists():
            raise CommandError(f"prefix {prefix!r} is already in use")

        token, pfx, secret_hash = mint(prefix)
        key = AgentApiKey.objects.create(
            prefix=pfx,
            secret_hash=secret_hash,
            label=opts["label"],
            scope=opts["scope"],
            user=user,
        )

        self.stdout.write(self.style.SUCCESS("API key created."))
        self.stdout.write("")
        self.stdout.write(f"  prefix : {key.prefix}")
        self.stdout.write(f"  scope  : {key.scope}")
        self.stdout.write(f"  acts as: {user.username}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("  token (shown once, store it now):"))
        self.stdout.write("")
        self.stdout.write(f"    {token}")
        self.stdout.write("")
        self.stdout.write("  usage:")
        self.stdout.write(f"    curl -H 'Authorization: Bearer {token}' \\")
        self.stdout.write("         http://127.0.0.1:8901/api/v1/me")
        self.stdout.write("")
        self.stdout.write(self.style.ERROR("  This token is not recoverable."))
