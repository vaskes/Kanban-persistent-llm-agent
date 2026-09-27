# The learning layer — design

This is the part that makes the system worth building rather than a task
tracker with an LLM stapled on. It is **not** in part 1. This document records
the design so the implementation does not drift from the reasoning.

## The problem, stated precisely

A naive design stores experience in Postgres and retrieves the top-k relevant
rows into the prompt at the start of each tick. **That does not work**, and it
is worth being exact about why:

KV cache is not free memory — it is spent context. Loading 10k tokens of
history leaves `ctx − 10k` for actual work, at any loading speed. Retrieved
rows are not free either: they are literally tokens in the prompt. A system with
256k context can hold a lot and still be useless, because the relevant fact is
one row among ten thousand and the cost is paid on every single tick, forever.

So the question is not "how do I retrieve the right thing". It is:

> **Which surface should this experience be written to, at what scope, for how
> long, and with what evidence — given that every surface except the weights
> costs context on every request?**

## The cost table that drives every decision

| Surface | Context cost | Paid | Owner |
|---|---|---|---|
| Prompt context | full price | every request | LLM |
| Retrieved memory | price of top-k | every request | LLM |
| **Skills / harness** | ~0 (a one-line manifest) | once, at compile time | CPU |
| **LoRA adapter** | **0** | once, at training time | GPU |
| Base weights | 0 | at merge, destructive | GPU |

Everything below is about routing experience into the cheap rows.

## Five surfaces

### 1. Skills — the cheapest, most under-used

If experience reduces to "how to do X", it is not memory and not weights, it is
**code**:

```python
# skills/dreamline/regen_with_quality_gate.py
# compiled from 14 successful item_30..item_43 runs
def regenerate_with_gate(prompt, min_score=8.0, max_iter=4): ...
```

The agent sees one manifest line — `available_skills: [dreamline.regen_v3]` —
and a function call instead of 3000 tokens of narrative experience. This is
what "not filling up the context" actually looks like in practice.

**Routing rule:** if you can extract a common shape from five successful runs,
it is a skill, not a memory row.

### 2. Memory rows, but compressed to a line

The problem with top-k retrieval is that it returns *similar text*, not
*useful text*. Those are different things. So the unit is a single line:

```
SKILL:dreamline.regen_v3 | GOTCHA:min_score 8.2 vs 8.5 is noise, use 8.0 | TIME:~3min/item
```

~30 tokens instead of 2000, with a hard budget: **digest ≤ 100 tokens regardless
of how much history exists**.

### 3. Trained digestor — the only thing that actually scales

Asking an LLM to summarise is asking it to guess what matters. Train it
instead:

```
(task_text, full_history, k)  →  ≤100-token situation digest
```

A seq2seq model trained on the system's own trajectories. Input is 20k tokens
of history, output is bounded at 100 tokens. **Context cost is independent of
how much experience has accumulated** — which is precisely the property the
whole design needs.

Trained on `(trajectory, digest_that_preceded_success)`. Negative examples are
mandatory — digests derived from failed attempts, labelled harmful — otherwise
the model learns to compress rather than to compress *well*.

Implementation on the existing stack: a small sequence-to-sequence encoder-
decoder (BART-small scale, ~300M) over pgvector memories, CPU-friendly,
retrained nightly.

### 4. LoRA — the only surface with genuinely zero context cost

Patterns that transfer to unrelated tasks must go into weights.

llama.cpp supports **per-request adapter selection**:

```bash
llama-server -m model.gguf \
  --lora common.gguf --lora task_dreamline.gguf --lora task_ops.gguf \
  --lora-scaled common.gguf 0.3 \
  --lora-init-without-apply      # all loaded, all disabled
```

```http
POST /v1/chat/completions
{"lora": [{"id": 0, "scale": 1.0}, {"id": 1, "scale": 0.0}, {"id": 2, "scale": 0.3}]}
```

A task-kind adapter can be switched on per request at zero context cost.
Caveat: requests with different LoRA configurations are not batched together —
irrelevant here, since the runtime uses `n_parallel=1`.

Forgetting is a real risk, not a theoretical one. LoRA + EWC
(`L = L_B(θ) + (λ/2)·Σᵢ Fᵢ(θᵢ − θ*ᵢ)²`) plus a 10–20% replay buffer of older
examples. Without this, the adapter degrades on general tasks within two to
three cycles.

### 5. The updater — the brain that chooses

Receives an experience and decides: skill? memory line? training example?
adapter? or discard?

Rule: **write to the smallest reversible surface that solves the problem.** Not
straight to weights.

## Gating: experience is not knowledge until it passes

Three independent gates, all required:

| Gate | Question | On failure |
|---|---|---|
| **Local repair** | does it fix the observed failure? | do not publish |
| **Held-out transfer** | does it help related future cases that were *not* used to form it? | do not generalise it |
| **Non-regression** | is anything that used to work still working — safety, latency, cost? | roll back |

Held-out transfer is the one that matters most. A skill distilled from two runs
is overfitting. A skill distilled from twelve, nine of which are on data that
did not exist when it was written, is a transferable pattern.

Also: **keep failures and contrast them.** More is learned from
"1024×768 scored 8.4, 512×512 scored 7.1, n=17" than from either observation
alone.

## Two loops

```
COLD (night, GPU, off the hot path)
  trajectories → salience → consolidate (dedup, contrast, keep failures)
  → route: skill? memory? LoRA? updater?
  → train (QLoRA + EWC + replay)
  → gate: repair / transfer / regression
  → commit adapter + report to operator    [rollback always available]

HOT (tick, milliseconds)
  claim → digest (trained, ≤100 tokens) → lora: [task 1.0, common 0.3]
  → execute → verify → append to DB
```

The hot loop **does not read history at all**. No top-k, no slots, no "let me
just load a bit more". Experience arrives through exactly two channels: a
100-token trained digest, and weights.

## Build order

| Step | What | Context cost | Complexity |
|---|---|---|---|
| 1 | **Skills** — compile repeated patterns into code | ~0 | low |
| 2 | **Digest line** — hard 100-token budget | ~30 | low |
| 3 | **Trained digestor** — seq2seq on own trajectories | ~100, fixed | medium |
| 4 | **LoRA + EWC + replay** | 0 | high |
| 5 | **Router** — automatic surface selection | 0 | high |

Steps 1–2 are 20% of the work for 80% of the effect. That is not a consolation
prize: in the self-evolving-agents literature, compiling raw experience into
skills and memory abstractions *is* the operation that converts experience into
behaviour.

## Honest status

Continuous weight-updating in production is still unusual. The alignment
community's own assessment is that it is not yet solved — turning deployment
trajectories into training examples and avoiding regression on general
capability are both open. SEAL is frontier work, and its own authors report
degradation of earlier tasks across sequential self-edits.

The order above is therefore not "nice first, working later" — it is exactly
the reverse. Cheap, fully reversible surfaces first; expensive, hard-to-roll-back
surfaces last.

## References

- [Continual Learning for Next-Generation Agents](https://jxzhangjhu.github.io/blog/2026/continual-learning-for-next-generation-agents/) — routing experience to the right update surface
- [SEAL: Self-Adapting Language Models](https://arxiv.org/html/2506.10943v1) — model-generated self-edits as fine-tuning data
- [The What & When of Self-Evolving Agents](https://xinmingtu.github.io/blog/2026/self-evolving-agents/) — persistence horizons
- [Continual Learning in Token Space (Letta)](https://www.letta.com/blog/continual-learning/) — the opposing view: learn in context, not weights
- [What is Continual Learning (Alignment Forum)](https://www.alignmentforum.org/posts/5mCJzimtNZc9o4e26/what-is-continual-learning-and-why-might-we-expect-to-see-it) — honest about what remains unsolved
