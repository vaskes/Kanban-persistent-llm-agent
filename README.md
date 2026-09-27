# Kanban-persistent-llm-agent

A persistent, self-running LLM agent whose task board *is* its state machine.

> **Proprietary. Publicly readable, not open source.**
> See [LICENSE](LICENSE). Public visibility grants viewing only — no running,
> copying, modifying, commercial use, non-commercial use, or use as ML
> training data. Ask first.

**Status: Part 1 complete — task tracker core. No agent runtime yet.**

| | |
|---|---|
| Tests | 127, **100% statement + branch coverage** |
| Board | Postgres 16 + pgvector 0.8.6 |
| LLM | local llama.cpp / vLLM / MiniMax / Qwen behind one interface |

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
