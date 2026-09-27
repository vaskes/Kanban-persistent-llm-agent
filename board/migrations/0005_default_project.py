"""
Create the default project and its backlog.

The default project is this repository. It is created here so a fresh install
has it without anyone running a command, and so the tree always has an anchor.

Idempotent: get_or_create on the fixed key, and it never rewrites a project
that already exists.

This must run AFTER the schema migration that made projects.created_by
nullable. A data migration that inserts a row cannot precede the migration
that relaxes the constraint the row would violate.
"""

from django.db import migrations

DEFAULT_KEY = "kanban-agent"
DEFAULT_NAME = "kanban-agent"
DEFAULT_REPO = "https://github.com/vaskes/Kanban-persistent-llm-agent"
DEFAULT_DESCRIPTION = (
    "This project is the kanban-agent repository itself. Work on the "
    "persistent agent — features, fixes, research — is tracked here."
)


def create_default_project(apps, schema_editor):
    Project = apps.get_model("board", "Project")
    Backlog = apps.get_model("board", "Backlog")

    project, created = Project.objects.get_or_create(
        key=DEFAULT_KEY,
        defaults={
            "name": DEFAULT_NAME,
            "description": DEFAULT_DESCRIPTION,
            "repo_url": DEFAULT_REPO,
            "is_default": True,
            "created_by": None,
        },
    )
    if not created and not project.is_default:
        project.is_default = True
        project.save(update_fields=["is_default"])

    Backlog.objects.get_or_create(
        project=project, is_default=True, defaults={"name": "Backlog"}
    )


def drop_default_project(apps, schema_editor):
    Project = apps.get_model("board", "Project")
    Project.objects.filter(key=DEFAULT_KEY, is_default=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("board", "0004_backlog_agent_task_assignee_task_backlog_chatsession_and_more")
    ]

    operations = [
        migrations.RunPython(create_default_project, drop_default_project),
    ]
