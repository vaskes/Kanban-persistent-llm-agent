"""Record the rescue-triage + untracked-migration commit as a kanban task."""

import os
import sys

import django
sys.path.insert(0, "/opt/mavis-agent/kanban-agent")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone
from board.models import (
    Actor, Autonomy, ChatMessage, ChatScope, ChatSession, Kind, Project, Status,
    Task, TaskEvent,
)

User = get_user_model()
project = Project.objects.get(key="kanban-agent")
backlog = project.backlogs.get(is_default=True)
operator = User.objects.filter(is_superuser=True).order_by("id").first()

TITLE = "Rescue triage: verify /opt/rescue-llmhost2-2026-10-20, commit 5 orphaned migrations"

ACCEPTANCE = (
    "Every item in the rescue bundle checked against the live repo. No unique code found. "
    "The 5 orphaned Django migrations (0001, 0003, 0006, 0007, 0008) are now tracked in git, "
    "verified against a fresh empty database and a fresh clone. Working tree clean."
)

BRIEF = """\
## Summary
Another agent rescued artefacts from llmhost2 into
`/opt/rescue-llmhost2-2026-10-20/` before the host was wiped. Triage
result: **no unique code**, but it did expose a real debt — five
Django migrations that had never been committed anywhere. Those are
now in git.

## What was in the rescue
```
kanban-agent-bundle/all-refs.bundle     2.5 MB, git bundle, all refs
kanban-agent-untracked/                 empty directory
untracked.tar                           150 KB, 9 files
```

### untracked.tar — 5 migrations + 4 patch files
| File | Verdict |
|---|---|
| `board/migrations/0001_initial.py` | **orphaned from git** — committed in `f772ddf` |
| `board/migrations/0003_agentapikey.py` | **orphaned from git** — committed in `f772ddf` |
| `board/migrations/0006_alter_project_created_by.py` | **orphaned from git** — committed in `f772ddf` |
| `board/migrations/0007_agent_model_fields.py` | **orphaned from git** — committed in `f772ddf` |
| `board/migrations/0008_agent_model_probe_fields.py` | **orphaned from git** — committed in `f772ddf` |
| `admin-layout.patch` | stale `git format-patch` export of `d4c1dfb` — already in repo |
| `agent-key.patch` | stale export of `fb882bd` — already in repo |
| `resources.patch` | stale export of `3821574` — already in repo |
| `mcp-llm.patch` | export of `4927e09f` — **commit not in the bundle**, see below |

### The bundle — nothing new
Cloned `all-refs.bundle` to a scratch repo and diffed its commit set
against the live repo: **43 commits in the bundle, 0 of them absent
from kanban-agent**. The bundle holds both parallel histories of the
project (the llmhost2 line `a84f8b3`/`df2b85f` and the sandbox line
`17b4fb3`/`9648aa8`) because the two were committed independently
before the host access existed. Both are ancestors of `f772ddf`.

### The one loose end: `4927e09f`
`mcp-llm.patch` is a format-patch export of commit `4927e09f`
("feat(resources): add LlmEndpoint back as a kind, add McpServer").
That commit is **not** in the bundle and not in the repo — it was
amended into `a84f8b3` / `17b4fb3` before the push, so the patch is
the only surviving artefact of that intermediate state.

Verified that everything the patch adds is present in the current tree:

```
class McpServer in board/models.py            -> 1
MCP_SERVER in board/models.py                 -> 2
board/migrations/0011_mcpserver_*.py          -> contains McpServer
McpServer registered in board/admin.py        -> 5 references
mcp_server in RESOURCE_KINDS                  -> present
```

Nothing was lost. The four patch files are now deleted from the repo
root; they were the old hand-rolled transport for moving changes
between the two hosts and are obsolete now that the host mesh and a
real git remote exist.

## The actual bug this found
Five migrations had existed only in the llmhost2 working copy since
2026-09-27. Consequences before the fix:

- `git clone` + `manage.py migrate` would stop at `0002_audit_trigger`
- Everything from `0003` onward was missing from any fresh deployment
- **The schema could not be rebuilt from source at all**

The running database was fine — it was built incrementally from the
working copy. Only reproducibility was broken. This is the failure
mode the host-access note warns about: *"a deployed working copy is
not its git remote"*.

## Verification before committing
```
showmigrations board              -> 0001..0011, no gaps, all applied
makemigrations --check --dry-run  -> No changes detected (no model drift)
fresh empty database + migrate    -> all 11 apply, exit 0
fresh git clone                   -> all 11 migration files tracked, 873 files
```

## Prevention
The migrations were written on llmhost2 and never staged because that
host's clone had no working `origin` (Gitea was mid-move) and the
GitHub push path was manual. Now that `origin` points at the llmhost1
Gitea and `github` at GitHub, and the host access bootstrap gives every
sandbox push access, the commit-after-write step is just `git add
board/migrations/ && git commit`.

**Check this on any future move:**
```bash
git ls-files --others --exclude-standard
git log <remote>..HEAD
```
Both must come back empty before a host can be considered migrated.

## Related
- Task `Migration: llmhost2 -> llmhost1` — the stack move itself
- Rescue source dir `/opt/rescue-llmhost2-2026-10-20/` retained on
  llmhost1; redundant now but harmless (2.5 MB)
"""

EVIDENCE = {
    "rescue_path": "/opt/rescue-llmhost2-2026-10-20/",
    "bundle_commits": 43,
    "bundle_commits_missing_from_repo": 0,
    "untracked_tar_files": 9,
    "migrations_recovered": [
        "0001_initial", "0003_agentapikey", "0006_alter_project_created_by",
        "0007_agent_model_fields", "0008_agent_model_probe_fields",
    ],
    "patch_files_deleted": [
        "admin-layout.patch", "agent-key.patch", "mcp-llm.patch", "resources.patch",
    ],
    "orphan_commit_4927e09f": "not in bundle; content verified present via 0011 + models + admin",
    "verification": {
        "showmigrations": "0001..0011 no gaps",
        "makemigrations_check": "No changes detected",
        "fresh_db_migrate": "11/11 applied, exit 0",
        "fresh_clone": "all migrations tracked, 873 files",
    },
    "commit": "f772ddf",
}


def main():
    task, created = Task.objects.get_or_create(
        project=project,
        title=TITLE,
        defaults=dict(
            kind=Kind.CHORE,
            status=Status.DONE,
            acceptance=ACCEPTANCE,
            goal=TITLE,
            autonomy=Autonomy.AUTO,
            created_by=Actor.OPERATOR,
            backlog=backlog,
            closed_at=timezone.now(),
        ),
    )
    if not created:
        task.status = Status.DONE
        task.closed_at = task.closed_at or timezone.now()
        task.save()

    session, _ = ChatSession.objects.get_or_create(
        scope=ChatScope.TASK, project=project, task=task, backlog=backlog,
        defaults=dict(created_by=operator),
    )
    if not session.messages.filter(role="agent").exists():
        ChatMessage.objects.create(session=session, role="agent", content=BRIEF)

    if not task.events.filter(event="seeded").exists():
        TaskEvent.objects.create(
            task=task, actor=Actor.SYSTEM, event="seeded",
            payload={"evidence": EVIDENCE,
                     "recorded_at": timezone.now().isoformat()},
        )

    print(f"{'NEW' if created else 'EXIST'} {task.id[:8]} {task.status} {task.kind}")
    print(f"brief chars: {len(BRIEF)}")
    print(f"tasks in project: {Task.objects.filter(project=project).count()}")
    print(f"chat messages:    {ChatMessage.objects.count()}")


if __name__ == "__main__":
    main()