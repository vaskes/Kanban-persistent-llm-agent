"""
Bootstrap: seed the kanban-agent DB with the full project history so any
future agent — including a fresh session that has only this DB to look at —
can pick up the project without re-deriving context from chat transcripts.

Idempotent: tasks are looked up by (project.key, title) and skipped on rerun.
Each task gets:
  - status / kind / acceptance
  - a TASK-scoped ChatSession with a single 'agent' role message containing
    a full markdown brief (decisions, files, gotchas, what's next)
  - a TaskEvent recording the milestone with structured payload
"""

import os
import sys

import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
sys.path.insert(0, "/opt/mavis-agent/kanban-agent")
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone
from board.models import (
    Actor, Autonomy, ChatMessage, ChatScope, ChatSession, Kind, Project, Status,
    Task, TaskEvent,
)

User = get_user_model()

PROJECT_KEY = "kanban-agent"

project = Project.objects.get(key=PROJECT_KEY)
backlog = project.backlogs.get(is_default=True)
operator = User.objects.filter(is_superuser=True).order_by("id").first()

assert operator is not None, "no superuser found; cannot attribute ChatSession"

# --- task brief definitions -------------------------------------------------

TASKS = [
    # === FOUNDATION (DONE before this session) =============================
    (
        "Phase 1: state machine, board view, agent API, auth, projects",
        Kind.CODE,
        Status.DONE,
        "Single `transition()` in board/state.py. All status changes go through it. "
        "`claim()` writes status directly so it can hold the FOR UPDATE SKIP LOCKED "
        "row lock while enforcing the budget guard. 11-column board. Lease/heartbeat/sweep.",
        """\
## Summary
The minimum-viable kernel of the project: state machine + agent API + WebUI.
Everything else in the project is built on this and assumes it works.

## Decisions
- **Single `transition()`** is the only path to mutate `Task.status`. Direct ORM
  updates bypass audit; the state machine enforces acceptance/evidence/autonomy/
  budget/deps/children/operator guards.
- **`claim()` writes status directly** (not via `transition()`) because it must
  hold a row-level lock acquired via `SELECT ... FOR UPDATE SKIP LOCKED` and
  re-enforce the budget guard.
- **Identity comes from the API key, never from the request body.** A worker
  cannot impersonate another worker.
- **404 vs 403**: cannot see card -> 404; can see but don't hold lease -> 403.

## Files
- `board/state.py` — the state machine
- `board/views.py` — board + per-project board (`?project=<key>`)
- `board/api.py` — `/api/v1/*` endpoints
- `board/permissions.py` — `tasks_visible_to(user)` is the single source of truth
- `templates/board/board.html` — 11 columns, project picker

## What was tested
- 622 tests total at the time of completion, 100% statement+branch coverage on
  all measured files (`board/`, `providers/`, `core/`).

## What to know before touching this
- Bypassing `transition()` for "performance" reasons breaks the audit trail
  AND the guard rails. Don't do it.
- If a new state guard is needed, it goes in `transition()` not in the caller.
""",
        {"tests": 622, "covered_files": ["board", "providers", "core"]},
        "phase1: state machine + board + agent API",
    ),
    (
        "Auth: session + self-registration, first registered user = admin",
        Kind.CODE,
        Status.DONE,
        "Fresh instance: anyone can register at /register/. The first user to register "
        "becomes admin via a post_save signal `promote_first_user`. Subsequent users "
        "see NOTHING until an admin grants read access. No password reset flow.",
        """\
## Summary
Self-service registration on a fresh deployment, with the bootstrap-safety
guarantee that the first registrant is the admin.

## Decisions
- **First user -> admin** via `promote_first_user` post_save signal. A fresh
  instance must not be exposed to an untrusted network before its owner has
  registered.
- **No password reset flow** — no mail path on this network, and a
  password-reset endpoint is a brute-force surface. Operators use
  `manage.py changepassword`.
- **Session auth, 14-day lifetime.** Long enough to survive a long weekend,
  short enough that stolen cookies age out.
- **No `/api/v1/*` GET for User list** to non-admins.

## Files
- `board/auth_views.py` — /login/, /register/, /logout/
- `board/models.py` — `promote_first_user` signal receiver
- `templates/board/login.html`, `register.html`

## Gotchas
- The first-user signal MUST run on user creation, not on a separate
  one-shot management command. If it ran on a command, an operator who
  forgets to run it has an unauthenticated open deployment.
""",
        {"first_user_admin_signal": "promote_first_user"},
        "auth: session + register + first-admin",
    ),
    (
        "Projects tree + per-project board picker (WebUI)",
        Kind.CODE,
        Status.DONE,
        "`/projects/`, `/projects/new/`, `/projects/<key>/{edit,delete,members}/`. "
        "Shared `templates/board/base.html`. `MembershipForm` does NOT exclude "
        "existing members. `board/?project=<key>` filters to one project. CSS "
        "bugfix: `<header class=\"phead\">` -> `<section>`, `.cols` flex-wrap.",
        """\
## Summary
Project is the unit of access control. Every Task belongs to a Project.
Every ChatSession anchors to a Project (and optionally a Backlog / Task).

## Decisions
- **`MembershipForm` does NOT exclude existing members** from its queryset.
  Exclusion hid the "already has access" confirmation message.
- **No `clean_name` on ProjectForm**: CharField strips whitespace before
  field validation; a whitespace-only check there would never run.
- **CSS scoping**: project header uses `<section class="phead">`, NOT
  `<header>`, because the global `header{height:52px;position:sticky}`
  rule was meant for the top nav and clipped the card header.
- **`.cols` is `flex-wrap: wrap`**, not horizontal scroll.

## Files
- `board/views.py` — `board()` accepts `?project=<key>`
- `board/forms.py` — `MembershipForm`, `ProjectForm`
- `templates/board/board.html` — project tabs + header + 11 cols
- `board/tests/test_board_project_ui.py` — 17 tests
""",
        {"tests": 17, "ui_quirks": "header->section, cols flex-wrap"},
        "projects: WebUI + per-project board",
    ),
    (
        "Agent model: provider factory, crypto, register_agent, probe",
        Kind.CODE,
        Status.DONE,
        "`model_provider` (local_llama/local_vllm/cloud) + `model_name` + "
        "`model_base_url` + `model_api_key_cipher` + `model_max_tokens` + "
        "`model_temperature`. `Agent.provider()` builds the right `AgentProvider`. "
        "`manage.py register_agent` auto-mints a WRITE-scope API key on creation. "
        "`--rotate-key` mints fresh + disables old. `--no-key` skips mint. "
        "`board/probe.py` + `manage.py probe_agent` + admin action `probe_reachability`. "
        "`model_checked_at` / `model_check_ok` / `model_check_error` with 24h window.",
        """\
## Summary
Agents are configured with their LLM backend; the system can reach out and
verify the backend is live; the operator has CLI + admin surfaces for both.

## Decisions
- **`Agent.provider()` factory** keeps provider-specific code out of the
  model class.
- **Credentials stored encrypted on Agent itself** (not shared Credential
  model): `set_secret(field, plaintext)` / `get_secret(field)` refuse
  non-`_cipher` field names so a typo can't store plaintext.
- **`AgentApiKey.user` nullable**, `AgentApiKey.agent` nullable. Auto-minted
  key has user=admin so it inherits admin's projects.
- **`register_agent` auto-mints on creation**: forgetting to mint a key
  left agents unable to authenticate.
- **Two reachability signals, two windows**: daemon heartbeat 120s, model
  probe 24h. `status` property takes the freshest of the two.

## Files
- `board/crypto.py` — Fernet via PBKDF2-HMAC-SHA256 x 390k
- `board/probe.py` — `probe_agent(agent)` direct httpx to `/v1/models`
- `board/management/commands/probe_agent.py` — CLI
- `board/management/commands/register_agent.py` — auto-mint on create
- `board/migrations/0007_agent_model_fields.py`
- `board/migrations/0008_agent_model_probe_fields.py`

## Gotchas
- `model_check_ok=True` is necessary but not sufficient for `is_reachable`:
  the timestamp must be within the window. After 24h without a probe, the
  agent shows as unreachable even though it was answering perfectly at the
  last probe. See the open task on durable probe scheduling.
""",
        {"tests_added": 73, "migrations": 2},
        "agent: model + crypto + register + probe",
    ),
    (
        "Resources tree: 6 concrete kinds via MTI (Host, Sandbox, LlmEndpoint, ComfyUiEndpoint, GitRepo, Browser)",
        Kind.CODE,
        Status.DONE,
        "Multi-table inheritance: `Resource` parent + 6 child tables. One FK from "
        "`ResourceAllocation` covers any kind. Two lifetimes (persistent/ephemeral). "
        "Capacity/free_slots arithmetic. `set_secret`/`get_secret` (refuse non-`_cipher` "
        "field names). `ResourceAllocation` XOR task|project. `Resource/Browser.save()` "
        "force ephemeral + copy pool_size->capacity.",
        """\
## Summary
Resources are system-wide, finite things that a task or project can claim.
One parent table + concrete children so a single FK can refer to any kind
without discriminator-tag games.

## Decisions
- **MTI, not generic FK**: MTI is one JOIN away. Generic-FK is 3 queries.
- **XOR allocation**: `ResourceAllocation` is either to a task OR a
  project, never both. Encoded in schema (CHECK constraint), not just code.
- **`set_secret`/`get_secret` refuse non-`_cipher` field names**:
  typo-proofing. A typo that targets `password` instead of
  `password_cipher` would otherwise silently store plaintext.
- **Resources consolidated under one sidebar entry** with kind column +
  kind picker.
- **Ephemeral resources copy `pool_size -> capacity` in `save()`** so
  an operator who edits `pool_size` doesn't have to remember to also
  edit `capacity`.

## Files
- `board/models.py` — `Resource`, `ResourceKind`, `ResourceLifetime`,
  Host, Sandbox, LlmEndpoint, ComfyUiEndpoint, GitRepo, Browser,
  ResourceAllocation
- `board/crypto.py` — `set_secret` / `get_secret`
- `board/migrations/0009_resource_browser_comfyuiendpoint_gitrepo_host_and_more.py`
- `board/tests/test_resources.py`

## Gotchas
- Resource rows can't be added via the MTI parent admin — Django would
  try to save a row in the parent table with no child. The kind picker
  template routes the operator to the subtype's admin.
""",
        {"kinds": 6, "tests": "test_resources.py"},
        "resources: MTI tree with 6 concrete kinds",
    ),
    (
        "Admin: 4-section sidebar + Resources kind picker + LlmEndpoint/McpServer",
        Kind.CODE,
        Status.DONE,
        "`admin.site.get_app_list` overridden to build exactly 4 sections in order: "
        "Human Resources / Projects / Agent resources / Usable Resources. "
        "`index_title=\"Operations\"`. Hidden via `@_hide_from_sidebar`: Host, Sandbox, "
        "ComfyUi, GitRepo, Browser, ResourceAllocation, LlmEndpoint. "
        "McpServer added: url, transport (http/sse/stdio), protocol_version "
        "(default 2024-11-05), api_key_cipher, declared_tools (JSONField).",
        """\
## Summary
The admin is the operator's escape hatch. It's for OPERATIONS: people,
projects, agents, resources. Tasks live in the project detail page; the
admin sidebar stays uncluttered.

## Decisions
- **Exactly 4 sections, no more, no less.** "Usable Resources" is the
  one section that covers all 7 resource kinds.
- **Hidden != unregistered.** Hidden subtypes stay in the registry so
  the change/history/delete URLs keep working.
- **`index_title = \"Operations\"`**: the page heading matters for
  operator mental model. "Operations" implies getting work done.

## Files
- `board/admin.py` — `get_app_list` override, `_hide_from_sidebar`,
  Resources admin with kind picker
- `templates/admin/board/resource/add_kind.html` — kind picker
- `board/migrations/0011_mcpserver_alter_resource_kind.py`
- `board/tests/test_admin_layout.py`, `test_resources_admin.py`

## What to know before touching this
- Adding a new model to a hidden model admin: still register it,
  decorate with `@_hide_from_sidebar`.
- Adding a new section: the 4-section rule is intentional.
""",
        {"sections": 4, "tests": "test_admin_layout.py (28+ tests)"},
        "admin: 4-section sidebar + kind picker",
    ),
    # === THIS SESSION (DONE) ==============================================
    (
        "Postgres: restore kanban-pg + auto-restart + healthcheck + deploy script",
        Kind.OPS,
        Status.DONE,
        "Container `kanban-pg` was Exited(255) for 3 days (host reboot, no "
        "restart policy). `sudo docker start kanban-pg` brought it back, data "
        "intact (volume `kanban-pgdata`). Then `docker rm` + recreate with: "
        "`--restart=always`, `--health-cmd \"pg_isready -U kanban -d kanban -h 127.0.0.1 || exit 1\"`, "
        "`--health-interval=10s --health-timeout=5s --health-retries=3 --health-start-period=30s`. "
        "Saved `deploy/kanban-pg.sh` (idempotent recreate, preserves data volume). "
        "Placed a copy at `/opt/kanban-agent/kanban-pg.sh`.",
        """\
## Summary
The Postgres container had been down for 3 days without anyone noticing
because the `kanban-web` service kept returning 500s to every request and
nothing in the system screamed about it. Three layers of recovery are now
in place so this doesn't recur.

## Failure mode that was discovered
- Host `llmhost2` was rebooted ~3 days ago
- Docker daemon came back (its own systemd unit has `Restart=always`)
- The `kanban-pg` container did NOT come back — it had no `--restart` flag
- `kanban-web` stayed up, kept returning 500s to every DB query
- Nothing paged anyone

## The three layers of recovery (now in place)
1. **`kanban-pg` container**: `--restart=always` -> Docker brings it up on
   host reboot. The data volume `kanban-pgdata` is preserved across
   container recreation.
2. **Healthcheck on the container**: `pg_isready` every 10s, 3 retries,
   30s start period. Docker restarts the container if Postgres stops
   accepting connections (not just on host reboot).
3. **`kanban-web` systemd unit**: `Requires=docker.service`,
   `After=docker.service`, `Restart=always` — already configured.

## Files / artifacts
- `deploy/kanban-pg.sh` — idempotent recreate script. Safe to re-run:
  removes the existing container, preserves the volume, recreates with
  all flags.
- `/opt/kanban-agent/kanban-pg.sh` — copy at the operator's expected path.

## What's still open (see the related READY task)
- **No external watchdog.** If the healthcheck fails AND the restart
  fails AND the container stays broken, nobody gets paged.
- **No scheduled probe of the model endpoints.** See the related
  Probe-durability task.
""",
        {"container": "kanban-pg", "image": "pgvector/pgvector:pg16",
         "volume": "kanban-pgdata", "host_port": 5433,
         "data_preserved": True, "deploy_script": "deploy/kanban-pg.sh"},
        "ops: postgres auto-restart + healthcheck + deploy script",
    ),
    (
        "Agent reachability: diagnose 24h probe window, re-probe to recover",
        Kind.ANALYSIS,
        Status.DONE,
        "Both agents (Ornith, Minimax) showed unreachable in admin. Root cause: "
        "`MODEL_REACHABLE_WINDOW_SECONDS = 24h`, last probe was 134h ago. Probe "
        "itself was successful (`model_check_ok=True`), but stale. `manage.py "
        "probe_agent` brought both back to `is_reachable=True`. Deeper issue: "
        "without a scheduled probe, the 24h window will trip again silently. "
        "Tracked as a separate READY task.",
        """\
## Summary
The admin was correctly reporting "unreachable" by its own definition —
but the definition was too strict. The system was working, the metric
was lying.

## Root cause
- `model_checked_at` was 134.5h old on both agents
- `MODEL_REACHABLE_WINDOW_SECONDS = 24h`
- `is_reachable` therefore returned False
- `model_check_ok` was True, but the timestamp was past the window
- Nothing scheduled a re-probe; the admin was the only thing asking
  "is the model still alive?"

## The fix
`manage.py probe_agent` ran both probes in 2 seconds; both are now
`is_reachable=True`.

## The real fix (tracked separately, status=READY)
Two layers:
1. **Cron / systemd timer** that runs `manage.py probe_agent` for all
   agents every hour. Even with the existing 24h window, the window
   would be 1h wide because the probe is always fresh.
2. **Window expansion + staleness indicator**: bump
   `MODEL_REACHABLE_WINDOW_SECONDS` to 7 days AND show a separate
   column / badge "last probed N days ago" when older than 1 day.

## Files
- No code changes in this task — the fix was a CLI invocation.
- Tracked follow-up will touch `board/admin.py` (staleness column),
  settings (window), and add a `kanban-pg-probe.timer` systemd unit.
""",
        {"last_probe_age_h": 134.5, "window_h": 24, "fix": "re-probe via manage.py"},
        "analysis: 24h probe window tripped after host downtime",
    ),
    (
        "Admin UX: Resource redirects to subtype admin + per-kind fieldsets",
        Kind.CODE,
        Status.DONE,
        "Base Resource admin change/delete/history views redirect to the matching "
        "subtype admin (host, sandbox, comfyui, gitrepo, browser, llmendpoint, "
        "mcpserver). That's where the structured fields (hostname, IP, "
        "ssh_key_cipher, login, password_cipher, ...) live. Added a `configure ->` "
        "column in the changelist. Defined fieldsets on every subtype admin so the "
        "structured fields are grouped into logical sections (Identity, Credentials, "
        "Endpoint, Repository, Container, Runtime).",
        """\
## Summary
The base Resource admin had no per-kind columns. An operator clicking
on a Host row in the unified list saw only the parent table fields and
had no way to edit hostname / IP / ssh_key. Two changes:

1. **Redirect.** Base admin change/delete/history views redirect to
   the matching subtype admin via `_KIND_TO_MODEL` mapping.
2. **Fieldsets on every subtype admin.** The structured fields are
   grouped into logical sections. Host: Identity / Credentials.
   Sandbox: Container image / Network identity / Credentials.
   ComfyUI: Endpoint. GitRepo: Repository / Authentication. LlmEndpoint:
   Endpoint. Browser: Runtime. McpServer: Endpoint / Tools.

A `Configure` column in the changelist makes the redirect target
visible without following the auto-redirect.

## Why this matters
The operator expects "click on a row -> see all the fields for this
thing". With MTI the data is split across two tables, and Django
admin doesn't know to render the child fields when you access the
parent. Without the redirect, the parent admin is a 6-field form
that's a dead end.

## Files
- `board/admin.py` — `_KIND_TO_MODEL`, `configure_url` column,
  `change_view` / `delete_view` / `history_view` overrides, fieldsets
  on every subtype admin
- `board/tests/test_resources_admin.py` — 17 new tests (per-kind
  redirect, configure column, fieldsets)

## What to know before touching this
- **If a new resource kind is added**, add it to `RESOURCE_KINDS` (the
  picker) AND register its admin (it'll be auto-hidden via
  `_hide_from_sidebar` if it follows the pattern). The redirect map
  `_KIND_TO_MODEL` is built from `RESOURCE_KINDS` so it picks up
  new kinds automatically.
- **Fieldsets must be updated when a new field is added to a subtype**.
  Without an explicit fieldset, Django renders all editable fields
  in a single "Fields" section — which works but loses the grouping.

## Commit
df2b85f on github.com/vaskes/Kanban-persistent-llm-agent
""",
        {"redirect_views": ["change", "delete", "history"],
         "kinds_redirected": 7, "tests_added": 17,
         "commit": "df2b85f"},
        "admin: Resource redirect to subtype + fieldsets",
    ),
    # === THIS TASK (IN_PROGRESS) ==========================================
    (
        "Bootstrap DB: seed project history tasks with full agent briefs",
        Kind.CHORE,
        Status.IN_PROGRESS,
        "Created comprehensive tasks in DB representing the full project history "
        "(foundation, this session's work, open items, future scope). Each task "
        "has a TASK-scoped ChatSession with an 'agent' role message containing a "
        "full markdown brief: summary, files touched, decisions, gotchas, "
        "what's next. Future agent can read the brief and pick up the project "
        "without re-deriving context from chat transcripts.",
        """\
## Summary
This task IS the bootstrap that creates the project history tasks and
their agent briefs. It's structured this way so that the very task
you're seeding appears in the seeded set, completing the recursion
without lying about its own state.

## Why this matters
A future agent that opens this project from a fresh session will have
only the DB to work from — no chat transcript, no memory. The briefs
in ChatMessage are the primary context document. Without them, the
agent starts from zero and re-derives (often wrongly) what was already
decided.

## Decisions
- **One ChatSession per task, scope=TASK** — the brief anchors to the
  task it describes. If the task is reopened later, the brief is
  already there as the "agent context".
- **`role='agent'`** even though these are auto-generated — they are
  written by me (the agent) about my own work. `role='system'` would
  imply an external seed; `role='agent'` is honest.
- **Idempotent on rerun** — tasks are looked up by
  `(project, title)`. The same script can be re-run after more
  history accumulates without duplicating.
- **TaskEvent for every DONE task** with a structured payload
  (`commit`, `tests_added`, `files`). The future agent can query
  TaskEvent for "what got done and when" without re-parsing briefs.

## Files
- `/workspace/seed/seed_history.py` (this script)
- Run via: `cd /opt/mavis-agent/kanban-agent && set -a && . ./.env && set +a && export POSTGRES_PORT=5433 && .venv/bin/python /workspace/seed/seed_history.py`

## What to know
- The script writes to the live DB. Re-run is safe (idempotent) but
  re-running won't update existing briefs — only fill in new tasks.
- If a task title changes in the future, update the script to use
  the new title and add a note in the corresponding ChatMessage.
""",
        {"seeded_tasks": 12, "script": "/workspace/seed/seed_history.py"},
        "chore: seed project history with agent briefs",
    ),
    # === OPEN / READY =====================================================
    (
        "Probe durability: scheduled probe (cron) + 7d window + stale indicator",
        Kind.OPS,
        Status.READY,
        "Without a scheduled probe, agent reachability silently expires after 24h. "
        "Add a `kanban-pg-probe.timer` systemd unit that runs `manage.py probe_agent` "
        "hourly. Bump `MODEL_REACHABLE_WINDOW_SECONDS` from 24h to 7d. Add a "
        "staleness column / badge in the admin that shows 'last probed N days ago' "
        "when the probe is older than 1 day, so the operator can see that "
        "reachability is real but stale, instead of a confusing false-negative.",
        """\
## Summary
Two-layer fix for the reachability-staleness problem surfaced in this
session. Both layers are needed: they cover different failure modes.

## Layer A: scheduled probe (systemd timer)
- New unit: `kanban-pg-probe.service` + `kanban-pg-probe.timer`
- `OnCalendar=hourly` so the probe stays within the existing 24h
  window with margin
- Runs as user `git` (or root) via `sudo -n`; needs access to `.env`
  and the venv
- On probe failure, exit non-zero so systemd records the failure
  (operator can `journalctl -u kanban-pg-probe`)
- Writable artifacts: `manage.py probe_agent` already updates
  `last_seen_at` / `model_checked_at` / `model_check_ok` /
  `model_check_error`

## Layer B: window expansion + staleness indicator
- Bump `MODEL_REACHABLE_WINDOW_SECONDS` from 24h to 7d
- Add a `model_age` display method on `Agent` admin: human-readable
  "5d ago" / "2h ago" / "fresh"
- When `model_checked_at` is older than 1d, show a "stale" badge
- The badge makes the failure mode legible: operator sees
  "reachable, last probed 5 days ago" instead of just "unreachable"

## Why both layers
- Layer A alone: if the timer breaks, the same outage recurs after 24h
- Layer B alone: the operator sees "reachable" forever after one
  probe, even if the model has actually gone down
- Together: timer keeps the window fresh; the badge makes
  timer-breakage visible (the badge ages but reachability stays
  True) so the operator notices "stale" and investigates the timer

## Files to touch
- New: `/etc/systemd/system/kanban-pg-probe.{service,timer}`
- `core/settings.py` — `MODEL_REACHABLE_WINDOW_SECONDS`
- `board/admin.py` — `model_age` column on AgentAdmin
- `board/models.py` — `model_age` property if needed
- `board/tests/test_model_reachability.py` — cases for 7d window +
  stale-but-reachable

## Acceptance
- `systemctl status kanban-pg-probe.timer` shows the timer active
- After deliberately breaking the timer (`systemctl stop`), the
  badge appears within 1d and reachability stays True for 7d
- Within 1h of restoring the timer, the badge clears
""",
        {"related_session_task": "Agent reachability: diagnose 24h probe window",
         "layers": ["systemd timer", "7d window", "stale badge"]},
        None,
    ),
    (
        "Watchdog: page operator when kanban-web or kanban-pg is broken",
        Kind.OPS,
        Status.READY,
        "Currently nothing pages the operator when Postgres stays down for 3 days "
        "after a host reboot. The Telegram bot already runs on the host (port 8000). "
        "Add a small systemd timer that runs every minute, checks `pg_isready` and "
        "`curl /healthz`, and sends a Telegram message on transition from "
        "healthy->unhealthy. Cooldown 5 minutes so the same outage doesn't spam.",
        """\
## Summary
The Postgres outage in this session was silent for 3 days. The web
tier kept returning 500s, but nothing paged anyone. A 30-line
shell script + a systemd timer closes the loop.

## Why this is its own task (not bundled with the probe-durability one)
- The probe-durability task is about model endpoints, not Postgres
- The watchdog is about the substrate (Postgres + web tier)
- They have different blast radii: model probe breaks -> agents fail
  to delegate, but the board still works. Postgres breaks -> entire
  product is dead.

## Sketch
```bash
# /opt/kanban-agent/bin/watchdog.sh
check() {
  pg_isready -h 127.0.0.1 -p 5433 -U kanban >/dev/null 2>&1 \\
    || { notify "postgres down on llmhost2"; return 1; }
  curl -fsS -m 5 http://192.168.10.7:8901/healthz >/dev/null 2>&1 \\
    || { notify "kanban-web /healthz failed"; return 1; }
  return 0
}
```
Wrap in a systemd timer (`OnUnitActiveSec=60`) with a state file
so transitions (not steady state) trigger the message.

## Files to touch
- `/opt/kanban-agent/bin/watchdog.sh` (new)
- `/etc/systemd/system/kanban-watchdog.{service,timer}` (new)
- Telegram bot config: the bot already has an HTTP API; just POST
  to it. No new auth needed.

## Acceptance
- `systemctl status kanban-watchdog.timer` is active
- `sudo docker stop kanban-pg` results in a Telegram message within
  60s
- `sudo docker start kanban-pg` results in a "recovered" message
- No spam: stopping Postgres twice within 5 minutes only sends one
  alert
""",
        {"outage_discovery_lag_h": 72,
         "uses_existing_infra": ["telegram-bot", "kanban-web healthz", "pg_isready"]},
        None,
    ),
    # === FUTURE / BACKLOG =================================================
    (
        "Part 2: WebUI surfaces for chat sessions at scope matrix (projects / backlog / task)",
        Kind.CODE,
        Status.BACKLOG,
        "ChatSession model already exists (scope=projects|backlog|task, CHECK "
        "constraint enforces anchor matches scope). ChatMessage has role "
        "(user/agent/system), agent FK, tool_calls JSONField. Missing: the "
        "WebUI surfaces (project detail page, backlog page, task page) that "
        "let the operator / agent attach and read chat history at the right "
        "scope, and the run loop that drains pending user messages into the "
        "agent runtime.",
        """\
## Scope
Part 2 of the project. The data model is in place; the WebUI and the
runtime tick are not. This task is the umbrella — the actual work
will decompose into 3-5 sub-tasks when started.

## Sub-tasks to anticipate
1. `/projects/<key>/` — project detail page with chat tab
2. `/projects/<key>/backlog/<id>/` — backlog page with chat
3. `/projects/<key>/task/<id>/` — task page with chat + history
4. The runtime tick — drains user messages into the agent provider,
   appends agent responses, optionally calls
   `board.state.transition()` based on agent actions
5. Operator-side controls: pause/resume agent on a project, view
   transcript, kick the agent

## Constraints
- Must respect the existing scope matrix (projects|backlog|task)
- Must respect the existing permission system
- Operator must remain able to do everything via the admin as today
  (the WebUI is additive, not replacing)
""",
        {"phase": "part2", "model_state": "complete", "ui_state": "absent"},
        None,
    ),
    (
        "Part 2: multiple agents per project + assignment combobox",
        Kind.CODE,
        Status.BACKLOG,
        "Today: each task has zero or one assignee. To support real delegation "
        "the operator needs to assign multiple agents to a project (with a "
        "declared role per agent — primary, reviewer, observer) and pick from "
        "them in the assignment combobox. Currently the combobox is "
        "Agent.objects.all() — fine while there's only one agent per project, "
        "but a project with 3 agents needs to show only agents assigned to "
        "this project.",
        """\
## Scope
Add `ProjectAgentMembership` (project, agent, role) and filter the
assignee combobox on task create/edit to only show agents that are
members of the task's project.

## Decisions to make when started
- Roles: primary / reviewer / observer? Or free-form tags?
- Should an unassigned task be claimable by ANY project member, or
  only by `role=primary` agents? (Affects claim() in board/state.py.)
- Per-agent project overrides (e.g. agent A is primary in project X
  but observer in project Y) — supported by the membership model
  natively, but the UI surface needs to make it discoverable.
""",
        {"phase": "part2", "depends_on": "Project model exists"},
        None,
    ),
    (
        "Part 3: learning layer (memory/retrieval/LoRA/EWC) — design doc, not impl",
        Kind.RESEARCH,
        Status.BACKLOG,
        "Two-model split for learnability: small trained digestor + executor with "
        "LoRA. Hot loop never reads DB history. Memory model already exists in "
        "schema (kind=fact|lesson|procedure|preference|gotcha) but nothing writes "
        "to it. Retrieval / LoRA / EWC are the big research questions.",
        """\
## Status
Research only. Schema is in place; no code writes to Memory yet.

## Key design questions to address
- What does "learning" mean here? (Storage of successful patterns,
  not weight updates in part 3.)
- What's the surface that the executor queries? (Likely: a
  `Memory.retrieve(query) -> list[Memory]` that ranks by
  confidence x use_count x success_rate.)
- When does memory get written? (After a task transitions to DONE
  with non-empty evidence? After verifier passes?)
- How does the digestor fit? (Trained offline on transcripts?)
- EWC: do we need elastic weight consolidation, or is plain LoRA
  enough? (Probably: we don't, until we actually have weights.)
""",
        {"phase": "part3", "model_state": "placeholder", "impl_state": "none"},
        None,
    ),
]


# --- run ---------------------------------------------------------------------


def upsert_task(title, kind, status, acceptance, brief, evidence, commit):
    """Create-or-update a task by (project, title). Returns the task."""
    task, created = Task.objects.get_or_create(
        project=project,
        title=title,
        defaults=dict(
            kind=kind,
            status=status,
            acceptance=acceptance,
            goal=title,
            autonomy=Autonomy.AUTO,
            created_by=Actor.OPERATOR,
            backlog=backlog,
        ),
    )
    if not created:
        task.status = status
        task.kind = kind
        # touch a no-op field so save() has something to write
        task.attention_reason = task.attention_reason or ""
    if status == Status.DONE:
        task.closed_at = task.closed_at or timezone.now()
    task.save()
    return task, created


def attach_brief(task, brief, evidence, commit):
    """Attach a TASK-scoped ChatSession with a single 'agent' role brief."""
    session, _ = ChatSession.objects.get_or_create(
        scope=ChatScope.TASK,
        project=project,
        task=task,
        backlog=backlog,
        defaults=dict(created_by=operator),
    )
    if not session.messages.filter(role="agent").exists():
        ChatMessage.objects.create(
            session=session,
            role="agent",
            content=brief,
        )
    if evidence is not None and not task.events.filter(event="seeded").exists():
        TaskEvent.objects.create(
            task=task,
            actor=Actor.SYSTEM,
            event="seeded",
            payload={"evidence": evidence, "commit": commit,
                     "seeded_at": timezone.now().isoformat()},
        )


def main():
    for title, kind, status, acceptance, brief, evidence, commit in TASKS:
        task, created = upsert_task(title, kind, status, acceptance, brief, evidence, commit)
        attach_brief(task, brief, evidence, commit)
        marker = "NEW" if created else "EXIST"
        print(f"  {marker:6s} {task.status:13s} {task.kind:8s} {title[:60]}")

    print()
    print("=== summary ===")
    for s in Status.values:
        n = Task.objects.filter(project=project, status=s).count()
        if n:
            print(f"  {s:13s} {n}")
    print()
    print(f"  chat sessions : {ChatSession.objects.filter(project=project).count()}")
    print(f"  chat messages : {ChatMessage.objects.count()}")
    print(f"  task events   : {TaskEvent.objects.count()}")


if __name__ == "__main__":
    main()
