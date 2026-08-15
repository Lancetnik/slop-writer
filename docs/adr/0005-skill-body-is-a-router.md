# The skill body is a router; command detail lives in references

`SKILL.md` had grown to ~350 lines: every command, every flag, a validation
checklist, and an error table, all in the file loaded before any work starts.
Reading it cost the same whether the task was "who forwarded my post" or
"schedule this draft", and the flags that actually matter (`--latest` vs
`--limit`, `--at` needing a UTC offset) sat among ~300 lines that the branch at
hand didn't need.

The body now carries only what **every** branch needs — run from the project
root, resolve `<skill_dir>`, pass `--channel` explicitly, how to report a
command's summary, and what to do when a command demands setup — plus a table
mapping intent → reference file → CLI. Command detail moved to one reference
per CLI: `references/scraping.md`, `references/querying.md`,
`references/publishing.md`, with the pre-existing `schema.md` and `markup.md`
as their second hop.

## Considered Options

- **Split by CLI — chosen.** Each file is exactly one branch of what the user
  asks for (collect data / interrogate data / publish), with its own access
  requirements, so the routing table needs no judgement call. A split by intent
  (analytics vs publishing) would have merged querying into scraping, and the
  SQL rules are what the agent most often needs *without* scraping.
- **One `commands.md` — rejected.** It only moves the wall of text one hop
  down; the agent still loads publishing rules to answer a metrics question.
- **A worked example per branch in the body — rejected.** It makes each branch
  runnable straight from `SKILL.md`, which defeats "read the reference first"
  exactly where the misleading flags live. The body keeps one example, showing
  the *shape* of an invocation (`uv run <skill_dir>/scripts/<cli>.py … --channel
  @name`) and nothing selection-specific.

## Consequences

- The `Validation` section is gone. Its checks re-ran a query to confirm the
  command that just printed a summary did what it said — cost without a
  decision behind it. The one live inference it carried, "zero rows means a bad
  handle", stopped being true once `_resolve_peer` made a typo exit 1 before
  the DB is created; zero posts now means an empty window, and `scraping.md`
  says so where `scrape` is documented.
- The `Common errors` table is gone. Each row moved next to the command that
  raises it — the `group` warnings in `scraping.md`, the FTS5 fallback in
  `querying.md` — so diagnosis arrives with the command instead of 200 lines
  away. Rows whose "fix" was to tell the user what the error already says were
  dropped.
- First-run setup left the skill entirely: it was `skills/setup-tg-analytic/`,
  a user-invoked skill (ADR-adjacent to 0003's split of read from write — the
  human, not the agent, holds the interactive login). `SKILL.md` says nothing
  about it: `_credentials` and `_require_session` carry the whole instruction,
  so it arrives with the failure that needs it, at zero context cost, and
  reaches users driving the CLIs by hand too. A line in the body would have
  been that message's second copy — one that only pays off in the runs where
  it never fires.

  **Superseded in part by Lancetnik/slop-writer#20**, which deleted that skill:
  setup is now `slop-writer init`, a terminal command. The *placement* argument
  above survives intact and got stronger — the remedy still travels only in the
  error text (`hint`), which is now the sole thing that tells an agent what to
  ask for. What changed is that credentials no longer pass through the model's
  context at all, which a skill could not offer however it was written.
- `.env.example` was deleted; setup writes `.tg-analytic/.env` itself. A
  template file only worked when the reader could find the skill directory,
  which is the thing that varies by install method.
- Adding a command is now a one-file edit in `references/`. `SKILL.md` changes
  only when a new *branch* appears — a fourth CLI, or a new always-true
  invariant.
