"""
The default project and its default backlog.

The default project is this repository. It must exist on every install in every
state — fresh, migrated, mid-upgrade, after someone deleted something by hand
— because it is the anchor the whole tree hangs from and the place work on the
system itself is recorded.

Two mechanisms, deliberately:

  * a data migration creates it on the way up, so a fresh install has it
    without anyone running a command;
  * a system check reports it missing, so a database that lost it is loud
    rather than quietly broken.

The creation is idempotent: get_or_create on the fixed key.
"""

from django.core.checks import Error, Warning, register

from .models import (
    DEFAULT_PROJECT_KEY,
    DEFAULT_PROJECT_NAME,
    DEFAULT_PROJECT_REPO,
    Backlog,
    Project,
)

DEFAULT_PROJECT_DESCRIPTION = (
    "This project is the kanban-agent repository itself. Work on the "
    "persistent agent — features, fixes, research — is tracked here."
)

def ensure_default_project(*, description: str = DEFAULT_PROJECT_DESCRIPTION) -> Project:
    """
    Create the default project and its default backlog if they are missing.

    Safe to call on every request path, every management command, every
    migration: it never modifies an existing project beyond filling in a
    missing description.
    """
    project, created = Project.objects.get_or_create(
        key=DEFAULT_PROJECT_KEY,
        defaults={
            "name": DEFAULT_PROJECT_NAME,
            "description": description,
            "repo_url": DEFAULT_PROJECT_REPO,
            "is_default": True,
            "created_by_id": _bootstrap_user_id(),
        },
    )
    if not created and not project.is_default:
        # Someone recreated the key by hand without the flag. The tree depends
        # on this row being the default, so restore it rather than carrying on
        # with a half-initialised anchor.
        project.is_default = True
        project.save(update_fields=["is_default"])

    if not project.backlogs.filter(is_default=True).exists():
        Backlog.objects.get_or_create(
            project=project, is_default=True, defaults={"name": "Backlog"}
        )
    return project

def _bootstrap_user_id() -> int | None:
    """
    Owner of the default project.

    The first account is an administrator (see signals), so on a fresh install
    the earliest user is a sensible owner. If the table is empty there is
    nobody to attribute it to, and created_by is nullable here by design.
    """
    from django.contrib.auth import get_user_model

    return get_user_model().objects.order_by("id").values_list("id", flat=True).first()

def get_default_project() -> Project:
    """The default project, or a freshly created one if it went missing."""
    return Project.objects.filter(is_default=True).first() or ensure_default_project()

def get_default_backlog(project: Project) -> Backlog:
    b = project.backlogs.filter(is_default=True).first()
    if b:
        return b
    b, _ = Backlog.objects.get_or_create(
        project=project, is_default=True, defaults={"name": "Backlog"}
    )
    return b

@register()
def default_project_exists(app_configs, **kwargs):
    """
    System check: the anchor must be present, and must not be duplicated.

    Returns nothing at all when the tables do not exist yet. Management
    commands run system checks before migrating, and a check that blows up on
    an unmigrated database makes `makemigrations` unrunnable.
    """
    from django.db import ProgrammingError

    try:
        defaults = list(Project.objects.filter(is_default=True))
    except ProgrammingError:
        return []  # not migrated yet; nothing to assert
    except Exception:  # noqa: BLE001 - a check must never break a command
        return [Warning(
            "Could not verify the default project.",
            hint="Run `python manage.py migrate` and re-run the check.",
            id="board.W002",
        )]

    problems = []

    if not defaults:
        problems.append(
            Error(
                "The default project is missing.",
                hint=(
                    "Run `python manage.py ensure_default_project`. Every install "
                    "must have it: it anchors the whole task tree and is where "
                    "work on this system is tracked."
                ),
                id="board.E001",
            )
        )
    elif len(defaults) > 1:
        problems.append(
            Error(
                f"{len(defaults)} projects are marked default; exactly one must be.",
                hint="Clear is_default on all but the one with key "
                     f"'{DEFAULT_PROJECT_KEY}'.",
                id="board.E002",
            )
        )
    else:
        project = defaults[0]
        if not project.backlogs.filter(is_default=True).exists():
            problems.append(
                Warning(
                    f"Project '{project.key}' has no default backlog.",
                    hint="Run `python manage.py ensure_default_project`.",
                    id="board.W001",
                )
            )

    return problems
