# `run_query` takes a batch, and a query's verdict is content

Every analytics question goes through one tool. The aggregate tools return
fixed-shape summaries, so anything more specific than what `summarize_*` prints
has to be SQL — which makes `run_query` the only arbitrary-data escape hatch and
the only tool carrying `_meta["anthropic/alwaysLoad"]`. A production session
(2026-08-15, 18:40 MSK) spent 16 of its 44 tool calls there, spread over 11 of
its 34 round trips, and each round trip re-read a ~111k-token prefix.

`run_query` now takes **`queries: list[Query]`** rather than a single `sql`, and
a statement the database refuses comes back as that question's section rather
than as a failed call. `isError` is reserved for the call itself refusing.

## Considered Options

- **A second `run_queries` tool — built first, then rejected.** It was the
  obvious shape: leave the working tool alone, add the batched one beside it.
  It fails on one fact that is invisible from the tool's own file — `run_query`
  is the *only* tool with `anthropic/alwaysLoad`, and the other ten are deferred
  behind `ToolSearch`. A twelfth tool would therefore have been the batched
  form nobody sees by default, reachable only by an agent that first went
  looking for a tool it had no reason to believe existed. The batch has to live
  in the loaded tool or it does not exist. It also puts the roster at twelve to
  express one argument's arity.
- **Keeping `sql` and adding an optional `queries` — rejected.** Two ways to ask
  the same question, one of which is always worse, and a description that has to
  explain when to use which. Every caller had to change anyway.
- **`isError: true` when every query in the batch failed — implemented, then
  reversed.** The rule was "the call produced no answers at all", which for one
  question degenerated exactly to the pre-batch behaviour. That backward
  compatibility bought nothing: the signature changed, so no caller survived
  unedited. What it cost was a flag meaning two unrelated things — *no database
  to look in* and *the SQL was bad* — of which only the second is fixed by
  reading the schema. It also forced an arbitrary choice for the all-failed
  batch: one `code` standing for two different refusals, with the causes spliced
  into a single `message` string. Reversing it deleted that code rather than
  replacing it.
- **A `SUCCESS`/`ERROR` header on every section, including a lone one —
  rejected.** Uniform outcome shape is worth having; uniform scaffolding is not.
  A single unlabelled question is the most common call there is, and `## 1.`
  orders nothing when there is one of them.

## The rule

**A query's verdict is content. `isError` is the call refusing.**

| call | `isError` | text |
| --- | --- | --- |
| one query, succeeds | `false` | the bare table |
| N queries, all succeed | `false` | numbered sections under their labels |
| N queries, one fails | `false` | sections, one of them `**failed** (CODE)` |
| one query, fails | `false` | `**failed** (CODE): …` plus the schema listing |
| N queries, all fail | `false` | every section `**failed**` |
| no database for the channel | `true` | `{code, message, hint}` — `NO_DATA` |

The seam is not a count. `NO_DATA` raises because there is no per-question
answer to give: the file was not there, so nothing was asked. Everything a
query can do to itself is that query's answer.

## Consequences

- **`QueryFailure` is carried, not raised.** `run_queries` returns
  `list[QueryResult | QueryFailure]` positionally — item *i* answers `sqls[i]`,
  successfully or not — so the caller can always line an answer up with its
  question. Only `_open_ro` raises, which is what makes "batch-wide" a property
  of the database rather than a tally.
- **`run_query` (singular, in `query.py`) survives for the CLI only.** It is the
  form that raises, because `tools/tg_query.py` reports a failure as a non-zero
  exit and a line on stderr — there is no section for a refusal to travel in.
  The server takes the batch form even for one question.
- **`label` is optional, and a lone unlabelled query renders as the bare
  table.** Numbering is positional and always printed once there is more than
  one section, because a silently skipped failure would shift every answer after
  it against the questions the caller still holds. But `queries` carries the
  single-question case too, and there a heading names something the caller asked
  one step ago and has not forgotten.
- **This tool's description and schema are the hot path, and nothing else's
  is.** `run_query` is the only always-loaded tool, so its ~470 tokens are paid
  every turn while the other ten cost nothing until `ToolSearch` fetches one —
  83% of the roster's text is free until asked for. Two consequences follow.
  A Pydantic model's **docstring ships as its schema `description`**, so
  rationale written for a reader of the code (`Query`'s original docstring) was
  being billed to the model on every turn; here it lives in a comment above the
  class instead. And the description must not restate what the `Field`s already
  carry — the schema is machine-readable and cannot drift from itself, whereas
  a second copy in prose can.
- **The skill needed no edit.** The seam holds — no metric fact in a tool
  description, no argument name in the skill — so a change that is entirely
  about an argument's arity touches `server.py` and nothing under `skills/`.
- **`ERROR_CODES` is unchanged.** The reversal above is what avoided a
  `BATCH_FAILED` code that would have diagnosed nothing.

## Evidence

The empirical case is **not** what this decision rests on, and recording that is
the point of this section — someone will re-run the experiment.

**Method.** The same analytics scenario (ten independent questions over a real
channel database: totals, per-post views, comment leaders, forward rates) given
to a subagent twice, through a shim exposing the shipped library: arm A one
question per invocation, arm B all ten in one. Identical prompts, identical
schema listing, identical instruction to resolve independent questions in as few
round trips as possible; the batch was read from stdin so the measurement was
about batching rather than about shell quoting (an earlier harness passed JSON
through argv and paid two extra round trips to it — discarded). Accounting
groups transcript blocks by `message.id` and unions them: counting lines
inflates round trips, and taking the first line per id drops every parallel tool
call after the first. Three replicates per arm, median reported — one run that
stalls moves a mean and says nothing about the contract.

Context at the start of each run: ~10.8k tokens.

| | arm A (one per call) | arm B (batched) | Δ median |
| --- | --- | --- | --- |
| round trips | 3 | 2 | −33.3% |
| tool calls | 10 | 1 | −90.0% |
| cache read (tok) | 59,675 | 44,764 | −25.0% |
| peak context (tok) | 26,704 | 22,382 | −16.2% |
| output (tok) | 2,379 | 2,386 | +0.3% |
| cost ($) | 0.196 | 0.138 | −29.8% |

Those medians overstate the case. Within-arm range against between-arm
difference:

| | arm A | arm B | |
| --- | --- | --- | --- |
| tool calls | 10 – 10 | 1 – 2 | **separated** |
| round trips | 2 – 3 | 2 – 3 | overlapping |
| cache read | 32,962 – 70,980 | 33,206 – 69,821 | overlapping |
| peak context | 22,138 – 26,713 | 22,382 – 25,057 | overlapping |
| cost | 0.136 – 0.224 | 0.113 – 0.181 | overlapping |

**Only the tool-call count separated.** Everything cost-shaped had a
run-to-run spread inside one arm wider than the gap between the arms. The cause
is structural rather than sampling: cache read accrues per round trip, and
whether the model needed a second or a third turn is closer to a coin flip than
to a property of the contract under test. Both arms sat at 2–3 round trips
because **parallel tool calls are the real incumbent** — arm A reached 5.00
calls per assistant turn, collapsing ten calls into two messages without any
help from the tool's signature.

At this size, the batch is not measurably cheaper.

**Why we expect it to pay at production size anyway.** The 18:40 session above
ran at ~111k tokens of average context. There, one extra round trip costs about
$0.056 in re-read prefix against roughly $0.0075 for the `tool_use` and
`tool_result` blocks themselves — a ~7× ratio, so the money is in round trips
and not in blocks. The experiment ran at ~25k, where that ratio is small enough
that turn-count noise swamps it. A re-run at 25k that finds nothing is the
expected result, not a refutation; the test worth running is at production
context.

**What the decision actually rests on.** One tool rather than twelve, with the
batch visible by default because it lives in the only always-loaded tool. Ten
calls collapsing to one keeps a turn well inside the lookback a cache breakpoint
searches for a hit. And a partial failure costs one section instead of every
answer standing beside it — which is a correctness property of the contract,
true at any context size, and the one thing the experiment could not have
measured because nothing in the scenario failed.
