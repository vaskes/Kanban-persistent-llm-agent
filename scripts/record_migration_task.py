"""Record the llmhost2 -> llmhost1 migration as a task in the kanban DB itself."""

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

TITLE = "Migration: llmhost2 -> llmhost1 (full stack: Postgres, code, venv, service)"

ACCEPTANCE = (
    "kanban-agent runs entirely on llmhost1. Postgres container with restart=always + "
    "healthcheck, DB restored with matching row counts, Python 3.12 venv rebuilt, "
    "gunicorn bound to 192.168.10.6:8901, 638 tests green at 100% coverage, both agents "
    "reachable. llmhost2 retains a rollback copy."
)

BRIEF = """\
## Summary
The whole stack moved from llmhost2 (being decommissioned) to llmhost1
(192.168.10.6). Postgres, application code, Python runtime, venv, systemd
unit, .env. Data restored with row-count parity. Verified live.

## Result on llmhost1
| Item | Value |
|---|---|
| Code | `/opt/mavis-agent/kanban-agent` (owner git:git, 141 MB) |
| DB | `kanban-pg` container, `pgvector/pgvector:pg16`, volume `kanban-pgdata` |
| DB bind | `127.0.0.1:5433` (loopback only, same as before) |
| Web | `kanban-web.service`, gunicorn 23.0.0, 3 workers, bound `192.168.10.6:8901` |
| Python | 3.12.15 (uv standalone build at `/opt/uv-python`) |
| Tests | 638 passed, coverage 100.00% (2038 stmts, 456 branches, 0 missed) |
| Agents | both reachable (`probe_agent` exit 0) |

## Row counts — identical on both sides
```
tasks=22  agents=2  projects=2  resources=7  chat_messages=15
users=3   task_events=52  agent_api_keys=1
```
Task status split: BACKLOG 5, DONE 10, READY 5, REVIEW 2.

## Non-obvious things that broke, and the fix

### 1. Python version — the real blocker
llmhost1 is Ubuntu 26.04. System Python is **3.14**; the project is pinned to
packages that predate 3.14 (`psycopg[binary]==3.2.3` has no cp314 wheel — PyPI
only offers 3.2.10+ for 3.14). Ubuntu 26.04 has no `python3.12` package.

**Fix**: `uv` standalone CPython 3.12.15 at `/opt/uv-python`. Installed uv to
`/usr/local/bin/uv`.

**Do not** try to reuse the llmhost2 `.venv` by rsync — its `pyvenv.cfg`
hardcodes `executable = /usr/bin/python3.12` which does not exist on the new
host. The venv was rebuilt from `requirements.txt`.

### 2. uv refuses to run under sudo -u
Three separate failures, in order:
```
failed to open file `/root/uv.toml`: Permission denied
  -> sudo preserves $HOME=/root; uv reads the config from HOME
failed to create directory `/opt/uv-python`: Permission denied
  -> UV_PYTHON_INSTALL_DIR pointed at a dir git could not create
```
**Fix**: create `/opt/uv-python` and the uv cache dir as root first, then
`chown git`, and run with an explicit environment:
```bash
sudo -u git env HOME=/home/git UV_CONFIG_FILE=/dev/null \\
  UV_CACHE_DIR=/tmp/uvcache-git UV_PYTHON_INSTALL_DIR=/opt/uv-python \\
  PATH=/usr/local/bin:/usr/bin:/bin uv pip install ...
```
Note: setting only `HOME` is NOT enough — uv also honours a pre-existing
`UV_CONFIG_FILE` from root's environment.

### 3. pypi.org is unusably slow from llmhost1
`https://pypi.org/simple/` took **45 seconds** to respond. Installing 50
packages was not converging.

**Fix**: `UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`.
Same host resolves in ~5 s. After switching: 50 packages resolved in 37 s,
installed in 165 ms. **Use the Tsinghua mirror for any pip work on llmhost1.**

### 4. Duplicate uv processes deadlocked on the venv lock
Several install attempts overlapped; uv blocks on an exclusive lock at
`.venv/.lock`, so a second invocation waits forever and the log looks hung.
**Fix**: kill all of them and remove `.venv/.lock` before a clean retry:
```bash
for p in $(pgrep -f "uv pip install"); do sudo kill -9 $p; done
sudo rm -f /opt/mavis-agent/kanban-agent/.venv/.lock
```

### 5. .env — only two lines may change
`DJANGO_SECRET_KEY` **must stay byte-identical**. `board/crypto.py` derives
the Fernet key from it via PBKDF2-HMAC-SHA256 x 390k. Changing it makes every
stored `*_cipher` column (agent API keys, resource credentials) permanently
undecryptable. Changed on this move:
```
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,192.168.10.6   # was .7
BIND_ADDR=192.168.10.6:8901                            # was .7
```
Everything else (`POSTGRES_*`, `AGENT_LOCAL_LLAMA_BASE_URL=http://127.0.0.1:8080/v1`,
lease/heartbeat) carried over unchanged — the local llama is on the same port on
the new host.

### 6. Ornith's model_base_url pointed at the dying host
`Ornith` (local_llama) had `model_base_url=http://192.168.10.7:8080/v1` — the
old host. `probe_agent` failed with `ConnectError: [Errno 111]`. Repointed to
`http://127.0.0.1:8080/v1`; both agents now OK. Worth re-checking this after any
host move: agent rows store absolute URLs and nothing validates them.

## Git hygiene problem found during the move
`origin` (Gitea on llmhost1) was at `9a7ee37` while the working copy was at
`5d3da6b`. **Seven commits existed only in the working tree**, including
migrations 0001, 0003, 0006, 0007 and 0008 that are still `??` untracked on
BOTH hosts. Pushed the commits; the untracked migrations are still a problem —
see the separate task.

## Rollback
llmhost2 still has everything, untouched:
- `/opt/mavis-agent/kanban-agent` (199 MB)
- `/tmp/kanban.dump` (129 KB, sha256 `e8078eb9...`) — pre-move DB
- `/opt/mavis-agent/kanban-agent/.env` (original, BIND_ADDR .7)
- docker volume `kanban-pgdata` intact, container stopped cleanly (`Exited (0)`)

Nothing was deleted on llmhost2. Rolling back = `docker start kanban-pg`,
restore the dump, `systemctl start kanban-web`.

## Commits
- `df2b85f` admin: Resource redirect to subtype + fieldsets
- `5d3da6b` scripts: seed_project_history.py
- `2c2741a` tests: cover Resource admin fallback branches (100% coverage restored)
"""

EVIDENCE = {
    "from_host": "llmhost2 (192.168.10.7)",
    "to_host": "llmhost1 (192.168.10.6)",
    "row_counts": {"tasks": 22, "agents": 2, "projects": 2, "resources": 7,
                   "chat_messages": 15, "users": 3, "task_events": 52,
                   "agent_api_keys": 1},
    "tests": {"passed": 638, "coverage_pct": "100.00", "stmts": 2038, "branches": 456},
    "python": "3.12.15 (uv standalone, /opt/uv-python)",
    "system_python_on_host": "3.14 (unusable — psycopg 3.2.3 has no cp314 wheel)",
    "pypi_mirror": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "pypi_direct_latency_s": 45,
    "db_dump_sha256": "e8078eb91c437702dda2ba0f43d38747c4cf5d9f82bf94c982f2386f288b978c",
    "web_bind": "192.168.10.6:8901",
    "commits": ["df2b85f", "5d3da6b", "2c2741a"],
    "rollback": "llmhost2 retained: code 199MB, /tmp/kanban.dump, volume kanban-pgdata",
}


def main():
    task, created = Task.objects.get_or_create(
        project=project,
        title=TITLE,
        defaults=dict(
            kind=Kind.OPS,
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
                     "migrated_at": timezone.now().isoformat()},
        )

    print(f"{'NEW' if created else 'EXIST'} {task.id[:8]} {task.status} {task.kind}")
    print(f"brief chars: {len(BRIEF)}")
    print(f"tasks in project: {Task.objects.filter(project=project).count()}")
    print(f"chat messages:    {ChatMessage.objects.count()}")


if __name__ == "__main__":
    main()