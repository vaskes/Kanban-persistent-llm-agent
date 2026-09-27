# Roadmap

## Part 1 — task tracker core ✅ DONE

Board, state machine, leases, budgets, operator control, providers, audit.

- [x] Postgres schema + Alembic-equivalent migrations (Django)
- [x] State machine with structural guards in one transition function
- [x] `claim()` via `SELECT ... FOR UPDATE SKIP LOCKED`
- [x] Lease / heartbeat / `sweep_leases()` crash recovery
- [x] DAG dependencies, `unblock_ready()`
- [x] Hard per-card budgets, autonomy levels (`AUTO` / `ASK` / `MANUAL`)
- [x] Append-only audit + Postgres trigger backstop
- [x] `AgentProvider` + Fake / LocalLlama (per-request lora) / LocalVLLM / cloud
- [x] WebUI: board, card detail, reports, Django admin
- [x] 127 tests, 100% statement and branch coverage
- [x] systemd unit, smoke script, migration verification

## Part 2 — operator surface + runtime

Goal: an operator can run the system; the runtime can pick work up on its own.

### 2a. WebUI on HTMX

- [ ] Interactive board: column lanes, move cards, live refresh via SSE
- [ ] Card detail: edit goal/acceptance, comment thread, dependency editor
- [ ] Operator controls: pause, resume, priority + reason, retry with budget,
      autonomy override, inject instruction
- [ ] INBOX triage queue — flagged when acceptance is missing or ambiguous
- [ ] Create task from the board (not just from the CLI)
- [ ] Chat panel with streaming, wired to a provider, for ad-hoc questions
- [ ] Reports: burn per card, stuck list, verdict pass rate, provider latency

### 2b. Runtime

- [ ] Tick loop: `sweep_leases` → `unblock_ready` → `claim` → execute → verify
- [ ] Executor contract: attempt writes evidence, never self-declares done
- [ ] Verifier: deterministic first (`command`, `file_exists`), `llm_judge` as
      fallback with adversarial framing
- [ ] Heartbeat emission every `TASK_HEARTBEAT_SECONDS` during work
- [ ] Global kill switch: stop in-flight, leases expire, cards return to `READY`
- [ ] Token accounting into `tasks.tokens_used`, enforced by `_g_budget`
- [ ] Dead-letter: cards that exhaust budget land in `FAILED` with a reason

### 2c. Task generation

- [ ] Decomposer: split a card that will not fit its budget, max depth 4
- [ ] Proposer: agent may suggest *new* work into a `PROPOSED` lane, never
      straight into `BACKLOG`, with a per-day cap and justification required
- [ ] Rejected proposals become operator `preference` memory

## Part 3 — the learning layer

Design is written up in [LEARNING-LAYER.md](LEARNING-LAYER.md). Build order is
deliberately cheap-surfaces-first.

- [ ] **Skills** — compile repeated successful patterns into code
- [ ] **Digest line** — hard 100-token budget on what reaches the prompt
- [ ] **pgvector retrieval** — memory search, re-ranked
- [ ] **Trained digestor** — seq2seq over own trajectories
- [ ] **LoRA + EWC + replay** — nightly, gated by repair/transfer/regression
- [ ] **Router** — automatic choice of update surface
- [ ] Feedback loop closed: outcome → memory → future behaviour

## Later

- [ ] Multi-worker (scale beyond `n_parallel=1`)
- [ ] git integration: card ↔ branch ↔ commit ↔ PR
- [ ] Role separation at the database level: `agent_runtime` cannot delete a
      `DONE` card, `reader` cannot write at all
- [ ] Packaging for the live host

---

## Definition of done, per phase

- [ ] Every guard has a test that fails when the guard is removed
- [ ] Coverage stays at 100% (`fail_under = 90` enforced in `.coveragerc`)
- [ ] `bash scripts/smoke.sh` passes against a live server
- [ ] Migrations apply and reverse cleanly on a real database
- [ ] README and the relevant `docs/` file updated
- [ ] Commits pushed with a message that explains *why*, not just *what*
