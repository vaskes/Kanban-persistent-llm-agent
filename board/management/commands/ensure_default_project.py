"""
Guarantee the default project and its default backlog exist.

Runs in the data migration, and is safe to run by hand at any time. Idempotent:
it creates what is missing and leaves what exists alone.
"""

from django.core.management.base import BaseCommand

from board.bootstrap import ensure_default_project


class Command(BaseCommand):
    help = "Create the default project and backlog if they are missing."

    def handle(self, *args, **opts):
        project = ensure_default_project()
        backlog = project.backlogs.filter(is_default=True).first()
        self.stdout.write(self.style.SUCCESS("default project is present:"))
        self.stdout.write(f"  key      : {project.key}")
        self.stdout.write(f"  name     : {project.name}")
        self.stdout.write(f"  repo     : {project.repo_url}")
        self.stdout.write(f"  backlog  : {backlog.name if backlog else '(none)'}")
