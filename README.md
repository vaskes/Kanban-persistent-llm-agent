# Kanban-persistent-llm-agent

A persistent, self-running LLM agent whose task board *is* its state machine.

> **Proprietary. Publicly readable, not open source. No free use granted.**
> © 2026 Kuduza Ai Lab — <https://kuduza.com>. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
> Public visibility grants viewing only — no running, copying, modifying,
> commercial use, non-commercial use, or use as ML training data. Ask first.

**Status: Part 1 complete — task tracker core. No agent runtime yet.**

| | |
|---|---|
| Tests | 255, **100% statement + branch coverage** |
| Board | Postgres 16 + pgvector 0.8.6 |
| LLM | local llama.cpp / vLLM / MiniMax / Qwen behind one interface |

## Authorship

Written by **Mavis Agent** (MiniMax M3.1 powered), working under the supervision
and direction of the repository owner **vaskes**, a member of **Kuduza Ai Lab**
(<https://kuduza.com>). The agent performed the implementation, testing and
documentation; the rights holder made the design decisions, set the licensing
terms, and decided what gets published.

Whether to permit use of this project — and on what terms — is the rights
holder's decision alone. **In this project they have elected not to grant free
use.** No open-source, free, or public-domain grant is made or implied. Details
in [NOTICE](NOTICE).

The board is the state store, not a UI over some other storage. Every state
change goes through one function, `board.state.transition()`, and every guard
lives there — so there is exactly one place to audit and one place to break.

## Why the guards are structural

An agent that reports "done" without proof is the failure mode this project
exists to prevent. So the rules are enforced by the transition table, not by
prompting the model to behave:

| Rule | Enforced where |
|---|---|
| cannot leave `INBOX` without acceptance criteria | `_g_acceptance` |
| cannot enter `REVIEW` without evidence | `_g_evidence` |
| `MANUAL` cards cannot be approved by an agent | `_g_manual_autonomy` |
| `max_attempts` / `max_tokens` are hard caps | `_g_budget` + `claim()` |
| a card cannot close while subtasks are open | `_g_no_children_running` |
| cancelling and failing require an operator | `_g_operator_only` |
| audit row exists for every status change | Postgres trigger, not app code |

## Board flow

```
INBOX → BACKLOG → READY → IN_PROGRESS → REVIEW → DONE → ARCHIVED
            ↓         ↓         ↓          ↓
         BLOCKED   BLOCKED   FAILED   NEEDS_HUMAN
```

## Execution lifecycle

- `claim()` uses `SELECT ... FOR UPDATE SKIP LOCKED` — concurrent workers never
  block each other and there is no external lock protocol.
- A claim takes a **lease** (`TASK_LEASE_SECONDS`, default 900s).
- `heartbeat()` refreshes liveness every `TASK_HEARTBEAT_SECONDS` (10s).
- `sweep_leases()` returns dead worker's cards to `READY` and bumps
  `stuck_score`. **A killed worker cannot strand a card.** `stuck_score` is
  cumulative on purpose and only decays on real progress, so a card that has
  died five times in a row stays visible to the operator.

## Access

Two ways in, and neither reaches the other:

| | Who | How |
|---|---|---|
| **Board UI** | the operator, in a browser | session cookie, `vaskes` / `kanbanadmin`, 14 days |
| **Agent API** | headless workers | `Authorization: Bearer kb_…`, no browser, no session |

Anyone can register at `/register/`, and a new account sees **nothing** — no
project, not even the default one — until an administrator grants it read
access. The **first** account to register becomes the instance administrator;
every later one is an ordinary user. There is no password reset: use
`manage.py changepassword` at the shell.

An API key's permissions are inherited from the account it is bound to, never
widened.

```bash
python manage.py create_api_key --user worker --label "dreamline regen"
```

Full reference, including the SQL for granting admin, the endpoint table and the
status codes a worker must handle: **[docs/ACCESS.md](docs/ACCESS.md)**.

## Where to look, and how to test it

### Run it

```bash
# 1. database
docker run -d --name kanban-pg \
  -e POSTGRES_USER=kanban -e POSTGRES_PASSWORD=kanban -e POSTGRES_DB=kanban \
  -p 127.0.0.1:5433:5432 -v kanban-pgdata:/var/lib/postgresql/data \
  pgvector/pgvector:pg16

# 2. app
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit
POSTGRES_PORT=5433 .venv/bin/python manage.py migrate
POSTGRES_PORT=5433 .venv/bin/python manage.py createsuperuser
POSTGRES_PORT=5433 .venv/bin/python manage.py runserver 127.0.0.1:8901
```

Or as a service — `deploy/kanban-web.service` (gunicorn, loopback only):

```bash
sudo install -m 644 deploy/kanban-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kanban-web
systemctl status kanban-web
```

### Where to click

| URL | What is there |
|---|---|
| `/` | the board — all 11 columns, priority, attempt counters, stuck badges |
| `/task/<id>/` | one card: goal, acceptance, evidence, live step, full audit trail |
| `/reports/` | burn per status, events per actor, tokens, attempts, stuck list |
| `/admin/` | full CRUD over tasks, attempts, events — filters, search, bulk edit |
| `/api/v1/me` | who this API key is (bearer token, no session) |
| `/healthz` | liveness probe, no auth |

### Run the tests

```bash
.venv/bin/python -m pytest              # 127 tests
.venv/bin/python -m pytest --cov        # 127 tests, 100% statement + branch
bash scripts/smoke.sh                   # live HTTP checks against a running server
```

`--cov` enforces `fail_under = 90` from `.coveragerc`; the suite currently sits
at 100%. `test_providers_http.py` starts a real local HTTP server rather than
mocking httpx, so the OpenAI-compatible wire format is genuinely exercised.

### Proving the guarantees hold

The interesting tests are the negative ones — they assert a rule the operator
depends on:

```bash
.venv/bin/python -m pytest -k "cannot or refused or exhausted or manual"
```

These cover: no acceptance means the card cannot leave the inbox; no evidence
means it cannot reach review; `MANUAL` cards cannot be approved by an agent;
budget caps cannot be walked past even by taking the card directly; a killed
worker's card returns to `READY` on its own; the audit trigger fires on raw
SQL that bypasses the application.

## Providers

One abstract method, so the runtime (part 2) is written against the contract
rather than a backend.

| `AGENT_PROVIDER` | Backend |
|---|---|
| `fake` | deterministic, for tests |
| `local_llama` | llama.cpp `llama-server`, supports per-request `lora` |
| `local_vllm` | vLLM |
| `minimax` / `qwen` | OpenAI-compatible cloud |

`LocalLlamaProvider.complete(..., lora=[{"id":0,"scale":1.0}])` is the hook the
learning layer needs: a task-kind-specific adapter switched on per request at
zero context cost.

## Running

```bash
# Postgres 16 + pgvector (llmhost2)
docker run -d --name kanban-pg -e POSTGRES_USER=kanban -e POSTGRES_PASSWORD=kanban \
  -e POSTGRES_DB=kanban -p 127.0.0.1:5433:5432 \
  -v kanban-pgdata:/var/lib/postgresql/data pgvector/pgvector:pg16

python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export POSTGRES_PORT=5433
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver 127.0.0.1:8901
```

`/` board · `/task/<id>/` card + audit trail · `/reports/` · `/admin/` full CRUD
over tasks, attempts, events — the operator's control surface.

## Tests

```bash
.venv/bin/python -m pytest      # 59 tests
```

Coverage: every transition guard, the lease/sweep/crash-recovery lifecycle, the
DAG dependency rules, budget enforcement, the provider contract, and the audit
trigger firing on raw SQL that bypasses the application entirely.

## Not in part 1

Agent runtime, memory/retrieval, digestor, LoRA training, continual learner.
Schema for `memories` already exists so that part 2+ does not re-migrate.
