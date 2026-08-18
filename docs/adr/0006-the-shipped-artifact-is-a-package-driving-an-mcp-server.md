# The shipped artifact is a PyPI package driving an MCP server

Until 0.3.0 the *skill* was the product: a directory of PEP-723 scripts the
agent drove through Bash, distributed by `npx skills add` and set up by a
second, user-invoked skill. From 0.4.0 the product is the **distribution**.
`uv tool install slop-writer` puts a console script on the machine;
`slop-writer install` writes the agent wiring, `slop-writer init` writes the
Telegram state, and `slop-writer serve --mcp` runs the **MCP server whose
eleven tools are the entire agent-facing surface**. `skills/slop-writer/`
survives as the *analytic* layer only — which tool answers which question,
what the numbers mean, and the invariants that break an answer silently — and
the CLIs move to `tools/`, dev-only and undocumented.

The move was worked ticket by ticket under
[Lancetnik/slop-writer#10](https://github.com/Lancetnik/slop-writer/issues/10);
this ADR records the shape it settled into. It supersedes nothing on its own:
the two decisions the move touched amend themselves in place — 0003 for the
write gate, 0005 for what the router routes to — so nothing here restates them.

## What forced it

- **A guard that did not travel.** 0003's Consequences said it outright: the
  human approval on publishing was a repo-local Claude Code permission, so
  "a consumer who wants an approval gate must configure their own". Bash
  permissions are per-*file*, and the file was one CLI with both a read and a
  write subcommand. The tool surface makes the split a **name** —
  `publish_*` — and `install` ships the three `ask` rules from the same module
  that registers the tools, so the gate arrives with the distribution instead
  of being homework (#18, #20).
- **Flags cost skill body.** Under the CLIs, every command's arguments had to
  be written down somewhere the agent reads, which is what grew the wall of
  text 0005 was created to cut. A tool publishes its own schema, so the
  arguments document themselves at the call site and the skill keeps only what
  a schema cannot carry (#21, #30).
- **Failures were stderr.** A script's error reached the model as prose with an
  exit code. The tools answer with `{code, message, hint}` over a closed
  16-code vocabulary, which is a token the model can branch on (#15, #35).
- **Setup was a second skill.** `setup-tg-analytic` existed only because the
  login needs a TTY the agent does not have. As a terminal command it stops
  passing credentials through the model's context at all (#19, #20).

## Considered Options

- **Keep the scripts as the agent surface, ship them better — rejected.**
  Everything above is a property of driving a CLI through Bash, not of how the
  CLI was packaged. The scripts survive as `tools/`, because `login` needs a
  TTY and because they are the only way to exercise Telethon without an MCP
  client; they are explicitly unsupported, and no file under `skills/` names a
  script or a flag.
- **Two products, package and skill, versioned apart — rejected (#21).** One
  version covers both, and both install channels (`slop-writer install`,
  `npx skills add`) serve the *same* `skills/slop-writer/` directory into the
  *same* `.claude/skills/slop-writer/`. There is no drift detection: the last
  writer wins on disk, which is only safe because there is one directory to
  win. The wheel carries `skills/` as build data (`[tool.uv.build-backend]
  data`) so `install` can copy it out.
- **Structured tool results — rejected (#12, #16).** Claude Code discards the
  content blocks when `structuredContent` is present, so structure travels
  *inside* text and `render.summarize_*` returns a string. The SDK's default is
  the lossy branch, which is why this is enforced twice — a local `_tool`
  wrapper pinning `structured_output=False`, and `assert_text_only` refusing to
  start a server whose tools carry an `outputSchema`.
- **Fewer, merged tools — deferred, then ruled out of scope (#23, #25).**
  Tool count was never the context lever: every tool past the fourth already
  sits behind `ToolSearch`. Whether eleven is the *right* eleven is a
  behavioural question that needs a surface people drive, and it is tracked
  outside this decision.
- **A remote/hosted server, or a bundled runtime — out of scope.** This
  decision only declines to foreclose them; `uv` is already assumed by the
  repo, and the server takes its project root explicitly (`--project`) rather
  than reading the cwd anywhere below the entrypoint.

## Consequences

- **Two front doors, one domain.** `server.py` and `tools/tg_*.py` are both
  argument parsing and rendering over `slop_writer.*`; #22 extracted the domain
  precisely so the server could be a second caller rather than a rewrite. The
  `X_with_client(client, …)` / `X(…, session_file)` twins exist for the same
  reason — a server holds one client for its lifetime and must not pay a login
  per request.
- **Existing installs migrate by doing nothing.** The state directory keeps its
  name (`.tg-analytic/`), `.env` keeps the three keys it always had and `init`
  preserves any it does not own, and a per-channel DB self-heals on `open_db`.
  The one artefact a pre-0.4 install leaves behind is the old skill directory,
  which `install` deletes and reports (#34) — two model-invocable skills
  claiming one job is not cosmetic when the stale one advertises CLIs that are
  now undocumented.
- **The suite is the second acceptance surface.** It asserts on return shapes
  and `SlopWriterError.code`, never on stdout — which is exactly why
  `summarize_*` becoming `-> str` broke nothing. A live run against
  @fastnewsdev is still the last check, but no longer the only one (#27, #28,
  #31, #41).
- **The read/write split now stops at the module.** `server.py` imports
  `publish.py` alongside the read paths; below the entrypoint 0003 is intact,
  at the entrypoint the name prefix carries it. A `publish_*` tool added
  without its `ask` rule fails the suite rather than shipping ungated.
- **Argument validation escapes the error contract.** Pydantic rejects a
  malformed `select` arm before the `_guarded` wrapper runs, so those failures
  come back as a pydantic message rather than `{code, message, hint}`.
  Recorded, not fixed: intercepting them means taking arguments as a raw dict,
  which discards the schema the tagged union exists to publish.
- **The skill's split is checkable in both directions**: no metric fact in a
  tool description, no argument name in the skill. That is what keeps a new
  tool from re-growing the text this move removed — a description has no room
  for a metric caveat, and the skill has no vocabulary for flags.

## Amended in 0.5 (Lancetnik/slop-writer, the Codex client)

Two sentences above were written when `install` knew one client, and
[0008](./0008-install-writes-more-than-one-client.md) makes them false as
written. The decision they belong to is unchanged: the shipped artifact is
still a package driving an MCP server, and the gate still arrives with the
distribution rather than as homework. It now arrives for more than one client.

- **"Both install channels serve the same directory into the same
  `.claude/skills/slop-writer/`"** — still true of those two channels, but
  `install` writes a second destination as well (`.agents/skills/`, which
  belongs to no client), so a project holds more than one copy of the skill.
  "There is one directory to win" no longer holds; one *source* does, and the
  copies are written from it in one run.
- **"#19 rejected shipping entries for Cursor, Codex and the Copilot coding
  agent: their launch cwd is unverified"** — Codex leaves that list. Cursor and
  the Copilot coding agent stay on it, and the printed entry stays printed and
  stays labelled unverified.

The claim that the gate "arrives with the distribution" was true of one client
and homework for every other; 0008 closes that gap rather than overturning it,
and adds the second emitter beside `permission_rules()` in the module that owns
the roster — for the same reason the first one lives there.
