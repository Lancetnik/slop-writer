# `install` writes more than one client, and the server module knows both

0.4 shipped one client. `slop-writer install` wrote Claude Code's project
configuration and, for anyone else, printed a JSON entry to paste by hand.
That fallback does not work for Codex at all: Codex is configured in TOML, so
the text on offer could not be pasted anywhere.

The cost of the workaround was not the typing. A Codex user who registered the
server themselves got the eleven tools with **no gate on the three publishing
ones** — the human approval that 0006 moved into the distribution went back to
being homework, and it went back silently. They also got no skill in a
directory Codex reads, so the agent called the metric tools without the
invariants that keep the numbers honest.

From 0.5 the command takes a repeatable `--client`, writes each selected
client's own wiring, and reports what landed for which. This decision records
the two shapes that a later reader is most likely to try to "fix": the gate
being emitted from `server.py`, and one project holding several copies of the
skill.

## The gate is translated in `server.py`, not in `install.py`

`server.py` already owned `WRITE_TOOLS` and `permission_rules()`, because a
tool renamed without its rule silently loses its gate — the roster and the rule
are one fact. Codex needs the same fact in a different vocabulary: Claude Code
matches a rule by the prefixed tool name, Codex matches a bare tool key under
its own server table.

```toml
[mcp_servers.slop-writer]
command = "slop-writer"
args = ["serve", "--mcp"]

[mcp_servers.slop-writer.tools.publish_schedule]
approval_mode = "prompt"
```

So `codex_approval_rules()` is a **sibling of `permission_rules()`**, and the
suite compares both against the roster and against each other. The obvious
alternative — a per-client adapter in `install.py`, or a `codex.py` beside it —
was rejected for the reason the first emitter is where it is: a second module
is a second place to forget, and a gate that is forgotten fails silently on the
one client nobody is looking at. The module that decides which tools write is
the module that says who has to approve them.

`approval_mode` accepts `auto`, `prompt`, `writes` and `approve`; **there is no
deny value**, so this axis can only put a human in front of a Telegram write.
Three explicit per-tool tables were chosen over one server-level
`default_tools_approval_mode = "writes"` because the explicit form names the
same three tools the roster names, which is what makes the invariant checkable.
A server-level default names no tool and so checks nothing.

*Open question, from the live run that has not happened yet:* whether `writes`
reads the MCP `ToolAnnotations` this server already declares. Those annotations
are empirically inert in Claude Code and set for spec-correctness alone; if
Codex reads them they are load-bearing on one client and decorative on another.
Recorded because the answer changes nothing here — the mode was rejected on a
separate ground — but it is worth knowing.

## Ownership, per client

| artifact | owner | idempotency |
| --- | --- | --- |
| `.mcp.json` entry | Claude Code | rewritten every run |
| `.claude/settings.json` permissions | Claude Code, the human's | first install only |
| `.claude/skills/<skill>/` | Claude Code | replaced wholesale |
| `CLAUDE.md` address block | Claude Code, the human's | first install only |
| `.codex/config.toml` server entry | Codex | rewritten every run |
| `.codex/config.toml` `tools` tables | Codex, the human's | first install only |
| `AGENTS.md` address block | no client | written when absent |
| `.agents/skills/<skill>/` | no client | replaced wholesale |

**Mixed ownership inside one file** is what is new. Claude Code keeps ours and
theirs in different files; Codex keeps the server entry and the approval
decisions in the *same* file, and Codex itself writes `approval_mode` there
when a human answers "don't ask me again". So the two existing policies meet
inside one file rather than either side of one, and first-install detection —
still "our own key is absent", still no marker file — is evaluated **per
client**, since a project can be a first install for one and an upgrade for
another.

The last two rows belong to no client on purpose. `AGENTS.md` is a cross-agent
convention rather than Codex's own file, and `.agents/skills/` is the one skill
root Codex discovers with no config layer involved — so an agent nobody
installed can still find the skill, and it keeps working in a directory the
human has not trusted. Only an unnarrowed `uninstall` removes them.

## Selection, never detection

`--client` is repeatable and accepts `claude` and `codex`. Omitted on
`install` it means `claude` alone, so a project that only ever used one client
holds exactly what it held before. Omitted on `uninstall` it means every client
— safe in a way the install default is not, because uninstall only ever removes
its own markers.

Autodetection was rejected: it would write artifacts nobody asked for, and it
would leave `uninstall` guessing which of them to take back.

## TOML without a dependency

Reading is `tomllib`, so keys the human wrote survive a merge. Writing is a
small emitter internal to `install.py` — small only because it emits **our
tables and nothing else**: the rest of the file is carried across as text, so a
comment three tables down comes out byte for byte. A parse-and-re-emit round
trip would need a *general* TOML writer (every type, every nesting) and would
rewrite the whole file to move one table.

Shelling out to Codex's own CLI was rejected twice over: it writes the global
config, and it exposes no flag for the approval keys.

The one form the emitter refuses is an entry the human wrote as an inline table
or a dotted key. Appending our `[table]` header beside it would be a duplicate
definition, which makes the whole file unreadable to Codex — so `install` names
the file and stops, the same call `_read_json` makes for corrupt JSON.

## Two things the Codex entry does not say

Both are deliberate omissions rather than oversights, and both are questions
for the live run rather than for this file.

- **No timeout.** The `.mcp.json` entry carries `timeout = 600000` because a
  scrape runs for minutes and a killed server mid-ingest is the expensive
  failure. Codex names and scales that idea differently
  (`startup_timeout_sec` / `tool_timeout_sec`), and an unverified key in a
  project layer risks the layer being rejected outright, which would take the
  gate with it. So the entry is written with `command` and `args` alone and the
  server inherits Codex's own default. **If a live scrape is cut short, the fix
  is `tool_timeout_sec` in `codex_server_entry()`** — recorded here so the next
  reader does not have to rediscover why the two entries disagree.
- **No `default_tools_approval_mode`.** The three `prompt` tables gate the
  three writes; the eight reads are unlisted, so they inherit whatever Codex
  defaults to. That is the half of the read/write split this decision does not
  *write* — the Claude Code side says `allow` on the whole server explicitly.
  It was left out because the four modes' semantics are documented by name
  only, and pinning reads to a value that turns out to mean something else
  would be worse than inheriting. **"A reading tool call raises no prompt" is a
  live acceptance check**, and if it fails the answer is a server-level default
  beside the three tables, not instead of them.

## Accepted costs

- **The one-directory-one-version rule loosens.** A project now holds two
  copies of the skill — `.claude/skills/` and `.agents/skills/` — and three in
  a checkout counting the source. 0006 could say "there is one directory to
  win" because there was; now there are several, and the last writer wins in
  each. They are written from one source in one run, so they cannot disagree
  about a *version*; they can disagree only if a human edits one, which
  `install` already says it overwrites.
- **One client's entry is committable and the other's is conditional.** Codex
  disables a project's configuration layer until the human trusts the
  directory, and the trust lives in *their* global config. So a teammate who
  clones gets a working `.mcp.json` and a `.codex/config.toml` that stays inert
  until they answer Codex's trust prompt. Reported by `install`, not worked
  around: this project does not write another tool's global file.
- **A managed Codex layer can override the gate without `install` noticing.**
  The file forms (`/etc/codex/managed_config.toml`, `~/.codex/…`) are checked
  and refused. The macOS MDM form is a managed *preference* rather than a file
  and cannot be detected by a path check — and it outranks everything, project
  layer included. Named here because the refusal above is otherwise easy to
  read as complete.
- **The global Codex config is never read for writing and never written.** It
  is the human's file by the same rule that makes the permission block theirs,
  and on a real machine it holds other servers' bearer tokens.
- **A key added *inside* our own entry does not survive an upgrade.** The
  server entry is rebuilt from `codex_server_entry()` every run and only the
  `tools` tables are carried across, so an `env` or a timeout a human added
  under `[mcp_servers.slop-writer]` is gone on the next install. Identical to
  what `.mcp.json`'s entry has always done, and the same sentence in the report
  covers it: overwritten every install, and that is the upgrade path.

## What this amends

0006 said the gate "arrives with the distribution instead of being homework".
That was true of one client and false of every other, which is the gap this
decision closes rather than a claim it overturns. Two of 0006's sentences no
longer read as written:

- "both install channels serve the *same* `skills/slop-writer/` directory into
  the *same* `.claude/skills/slop-writer/`" — still true of those two channels,
  but `install` now writes a second destination as well. See the cost above.
- "`install` … rejected shipping entries for Cursor, Codex and the Copilot
  coding agent: their launch cwd is unverified". Codex leaves that list.
  **Cursor and the Copilot coding agent stay on it**, and the printed entry
  stays printed and stays labelled unverified.

The launch-directory question that kept Codex off the list is answered
conservatively rather than proven: the entry is written path-free, exactly like
Claude Code's, and `test_the_codex_entry_carries_no_machine_specific_string`
pins that claim so a live run that contradicts it forces a deliberate change —
`--project <root>` in the args, and a report that stops calling the entry
portable.
