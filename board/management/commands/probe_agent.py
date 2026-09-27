"""
Probe one or all Agent rows from the CLI.

Useful from cron for liveness monitoring, and as the operator-facing entry
point for "is the model I just registered actually reachable?". The exit
code is 0 iff every probed agent answered; a non-zero exit makes it usable
as a healthcheck.

Usage:

    .venv/bin/python manage.py probe_agent              # probe all
    .venv/bin/python manage.py probe_agent --name Ornith
    .venv/bin/python manage.py probe_agent --name Minimax --name Ornith
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from board.models import Agent
from board.probe import probe_agent


class Command(BaseCommand):
    help = "Probe registered Agent rows' model endpoints."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--name", action="append", default=[],
            help="probe only these names (may repeat); default = all",
        )

    def handle(self, *args, **opts):
        import logging
        logging.getLogger("httpx").setLevel(logging.WARNING)

        qs = Agent.objects.all()
        names = opts["name"]
        if names:
            qs = qs.filter(name__in=names)
            missing = set(names) - set(qs.values_list("name", flat=True))
            if missing:
                raise CommandError(
                    f"no such agent(s): {sorted(missing)}; "
                    f"registered: {sorted(Agent.objects.values_list('name', flat=True))}"
                )

        if not qs.exists():
            self.stdout.write("(no agents registered)")
            return

        failures = 0
        for a in qs.order_by("name"):
            r = probe_agent(a)
            if r["ok"]:
                models = r.get("models") or []
                preview = ", ".join(models[:3]) or "(no model ids)"
                self.stdout.write(self.style.SUCCESS(
                    f"{a.name:24s} OK     {a.model_base_url}  models=[{preview}]"
                ))
            else:
                failures += 1
                self.stdout.write(self.style.ERROR(
                    f"{a.name:24s} FAIL   {a.model_base_url}  {r.get('error', '')}"
                ))

        if failures:
            raise CommandError(f"{failures} agent(s) unreachable")
