# Architecture

## The one idea

**The board is the state machine, and the state machine is the control point.**

There is no hidden in-process agent state, no side-channel where the runtime
keeps "what I was doing". Every fact about work in flight lives in a row in
Postgres. That is what makes three things possible at once:

1. an operator can see exactly what the agent is doing and why,
2. a worker process can be killed at any instant without losing or stranding work,
3. the audit log is complete, including changes made outside the application.

## Layers

```
┌─ operator ────────────────────────────────────────────┐
│  WebUI (HTMX) · Django admin · reports                 │
└──────────────────────┬─────────────────────────────────┘
                       │  views / forms / controls
┌─ board ──────────────▼─────────────────────────────────┐
│  state.py   transition()  ← the ONLY way to move a card│
│             claim() · heartbeat() · sweep_leases()      │
│  models.py  Task · TaskDep · TaskEvent · Attempt        │
└──────────────────────┬─────────────────────────────────┘
                       │  ORM over psycopg
┌─ postgres ───────────▼─────────────────────────────────┐
│  tables · SKIP LOCKED queue · audit trigger (0002)      │
│  pgvector (schema ready, unused until part 3)           │
└──────────────────────┬─────────────────────────────────┘
                       │
┌─ providers ──────────▼─────────────────────────────────┐
│  AgentProvider: one method, four implementations        │
└─────────────────────────────────────────────────────────┘
```

## Why one transition function

`board/state.py::transition()` is the single entry point for every state
change, and every guard in the system is attached to it. The alternative —
scattering status checks across views, the runtime, and management commands —
produces code where the rules are enforced in three places and all three must
agree.

With one entry point there is exactly one place to audit and exactly one place
to break. `TransitionError` carries a machine-readable reason, so the operator
interface can show *why* a card did not move rather than "operation failed".

## The guards, and what each one prevents

| Guard | Prevents |
|---|---|
| `_g_acceptance` | an agent inventing scope — a card cannot start undefined |
| `_g_evidence` | "done!" with nothing to show for it |
| `_g_manual_autonomy` | an agent approving work the operator reserved for themselves |
| `_g_budget` (×2: transition + claim) | unbounded token/cost burn per card |
| `_g_deps_closed` | executing a task whose inputs do not exist yet |
| `_g_no_children_running` | closing a parent that silently dropped subtasks |
| `_g_operator_only` | an agent cancelling or failing work on its own initiative |

Two of these deserve a note.

**Budgets are enforced twice, deliberately.** `claim()` writes status directly
because it must hold a row lock. That path would otherwise skip `_g_budget`
entirely — which is exactly the bug the first test run caught. Any code that
writes `status` outside `transition()` must re-check the guards itself, and
`claim()` now does.

**`stuck_score` is cumulative on purpose.** It accumulates on lease expiry and
decays only on real progress. Resetting it on claim — the obvious thing to do —
means a card that has died five times in a row looks exactly as healthy as a
fresh one, which defeats the point of tracking it.

## Concurrency

`claim()` uses `SELECT ... FOR UPDATE SKIP LOCKED`:

```sql
SELECT ... FROM tasks WHERE status = 'READY' ORDER BY priority DESC, created_at ASC
  FOR UPDATE SKIP LOCKED LIMIT 1
```

Two workers never block each other and never claim the same card. There is no
external lock protocol, no lockfile, no lease broker. Adding workers later is a
deployment detail, not a redesign.

**One lease, one heartbeat.** A claim takes a lease (`TASK_LEASE_SECONDS`, 15
min by default) and refreshes it every `TASK_HEARTBEAT_SECONDS` (10s).
`sweep_leases()` returns expired cards to `READY` and increments
`stuck_score`. Consequence: **a worker killed with SIGKILL cannot strand a
card**, and no operator action is needed to recover.

## Audit integrity

Application code writes `TaskEvent` rows with actor and reason. Migration
`0002` adds a Postgres `AFTER UPDATE` trigger that writes its own row whenever
`status` changes, regardless of origin. A direct `UPDATE` from `psql` still
produces an audit row.

The duplication is intentional: the application row is informative, the trigger
row is the tamper-evident floor. Tests assert the trigger fires on raw SQL and
stays silent on non-status updates (heartbeats must not flood the log).

## Provider contract

```python
class AgentProvider(abc.ABC):
    @abstractmethod
    def complete(self, messages, *, model, temperature, max_tokens) -> Completion: ...
```

One method, on purpose. Tool loops, retries, streaming and budget accounting
belong to the runtime, not the provider, so swapping MiniMax for a local
llama.cpp does not touch runtime code.

`LocalLlamaProvider.complete(..., lora=[{"id": 0, "scale": 1.0}])` sends
llama.cpp's per-request adapter list. This is the hook the learning layer needs:
switching a task-kind-specific adapter costs **zero context tokens**, because
the knowledge lives in weights rather than in the prompt.

## Data model

| Table | Purpose |
|---|---|
| `tasks` | one row per card; the unit of work *and* the unit of control |
| `task_deps` | DAG edges; a card is `READY` only when all deps are `DONE` |
| `task_events` | append-only audit, written by app *and* by trigger |
| `attempts` | one row per execution attempt |
| `memories` | placeholder so part 3 does not re-migrate |

`memories` exists now and is empty on purpose. The learning layer is the whole
point of the project, and forcing a migration at the moment the system first
carries real traffic is the wrong time to discover a schema problem.

## Decisions and their reasons

| Decision | Why |
|---|---|
| Postgres, not SQLite | concurrent worker + operator writes; `SKIP LOCKED`; triggers; roles; `pg_stat_activity` |
| `FOR UPDATE SKIP LOCKED` over a lockfile | atomic, no protocol to implement wrong, standard |
| Django + HTMX | Django admin gives full CRUD over every table for free — the operator control surface is the requirement, and this halves the UI work |
| append-only events, not status columns | history is the product; "who moved this card" must always be answerable |
| guards in one function | one place to audit, one place to break |
| budgets as columns, not conventions | a cap that is not in the schema is a cap that will be bypassed |
| `memories` table in part 1 | do not re-migrate under live traffic |
