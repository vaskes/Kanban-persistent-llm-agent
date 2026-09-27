# Changelog

All notable changes. Format follows Keep a Changelog; versioning is not
meaningful yet while part 1 is the only shipped phase.

## [Unreleased] — Part 1, task tracker core

### Added
- Board schema: `Task`, `TaskDep` (DAG), `TaskEvent` (append-only), `Attempt`,
  `Memory` (placeholder for part 3).
- State machine with every status change routed through a single
  `transition()` function, with structural guards: acceptance required to leave
  `INBOX`, evidence required to enter `REVIEW`, `MANUAL` autonomy not
  self-approvable, hard per-card budgets, dependency closure, no closing a
  parent with open children, operator-only cancel/fail.
- `claim()` on `SELECT ... FOR UPDATE SKIP LOCKED` with lease acquisition.
- `heartbeat()` liveness signal and `sweep_leases()` crash recovery: a killed
  worker returns its card to `READY` and increments `stuck_score`.
- `unblock_ready()` for DAG edges.
- `AgentProvider` contract with `FakeProvider`, `LocalLlamaProvider` (supports
  llama.cpp per-request `lora`), `LocalVLLMProvider`, `CloudProvider`
  (MiniMax, Qwen), and a `build_provider()` factory.
- Postgres audit trigger (migration 0002): a status change cannot happen in the
  database without an audit row, even via raw SQL.
- WebUI: board, card detail with audit trail, reports, Django admin.
- systemd unit, `scripts/smoke.sh`, `.env.example`, deployment docs.
- 127 tests at 100% statement and branch coverage.

### Fixed
- `claim()` bypassed the budget guard by writing status directly, so the
  attempt/token cap could be walked past. It now checks both and records a
  `claim_refused` event.
- `claim()` reset `stuck_score`, so a card that had failed repeatedly never
  looked stuck. It is now cumulative and decays only on real progress.
- Migration 0002's trigger inserted into a column named `task`; Django names
  foreign keys `task_id`. The trigger errored on every status change.
- `/reports/` used `Task.events` (a descriptor) instead of `TaskEvent.objects`
  and returned 500. Caught by the smoke test, not by the unit tests; a view
  test suite was added in response.

### Known limitations
- Board UI is server-rendered and read-mostly; interactive controls land in
  part 2a.
- No agent runtime yet — nothing claims cards outside of tests.
- `memories` table exists but is unused; the learning layer is part 3.
