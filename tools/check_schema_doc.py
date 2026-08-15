# /// script
# requires-python = ">=3.11"
# dependencies = ["slop-writer"]
#
# # Pinned to *this checkout*, not to PyPI, and unversioned for the same
# # reason: a drift guard that read the published schema would compare the
# # docs against whatever was last released and pass while the working tree
# # disagrees. uv resolves this path relative to the script file, so it holds
# # from any cwd. Only the source repo runs this, so the local pin costs
# # nothing.
# [tool.uv.sources]
# slop-writer = { path = "../", editable = true }
# ///
"""Guard against drift between SCHEMA + FTS_SCHEMA (the source of truth,
in slop_writer/db.py) and the DDL that references/schema.md restates for the
SQL-writing agent.

Dev-only tooling: skill *users* never run this. It guards the source tree
while the skill is being developed, so it lives in tools/ — which since #30
is where every dev-only script lives, the skill directory having been reduced
to the five files the agent reads. Run after editing either side:

    uv run tools/check_schema_doc.py

Exits 0 when every statement matches (modulo whitespace and IF NOT EXISTS),
1 with a per-statement diff otherwise. The doc's "Full schema at a glance"
block plus its per-table blocks are all checked - each must restate its
statement exactly.
"""

import re
import sys
from pathlib import Path

from slop_writer.db import FTS_SCHEMA, SCHEMA

SCHEMA_MD = (
    Path(__file__).resolve().parent.parent
    / "skills" / "slop-writer" / "references" / "schema.md"
)


def normalize(stmt: str) -> str:
    """Whitespace- and IF NOT EXISTS-insensitive form of one DDL statement."""
    stmt = re.sub(r"\bIF NOT EXISTS\b\s*", "", stmt, flags=re.IGNORECASE)
    return " ".join(stmt.split()).rstrip(";").strip()


def split_statements(sql: str) -> list[str]:
    """Split on ';', except inside CREATE TRIGGER BEGIN...END bodies —
    a trigger keeps its internal semicolons and stays one statement."""
    out: list[str] = []
    buf = ""
    for frag in sql.split(";"):
        buf = f"{buf};{frag}" if buf else frag
        flat = " ".join(buf.upper().split())
        if flat.startswith("CREATE TRIGGER") and not flat.endswith(" END"):
            continue
        out.append(buf)
        buf = ""
    if buf.strip():
        out.append(buf)
    return out


def statements(sql: str) -> set[str]:
    return {normalize(s) for s in split_statements(sql) if normalize(s)}


def main() -> int:
    truth = statements(SCHEMA + FTS_SCHEMA)

    doc = SCHEMA_MD.read_text(encoding="utf-8")
    doc_sql = "\n".join(re.findall(r"```sql\n(.*?)```", doc, flags=re.DOTALL))
    # Per-table blocks repeat statements from the glance block; common-join
    # examples are SELECTs - keep only CREATE statements.
    documented = {s for s in statements(doc_sql) if s.upper().startswith("CREATE")}

    missing = truth - documented
    stale = documented - truth
    if not missing and not stale:
        print(f"OK: schema.md matches SCHEMA ({len(truth)} statements)")
        return 0

    for s in sorted(missing):
        print(f"NOT IN schema.md:\n  {s}\n", file=sys.stderr)
    for s in sorted(stale):
        print(f"STALE in schema.md (not in SCHEMA):\n  {s}\n", file=sys.stderr)
    print(
        "Fix references/schema.md (or slop_writer/db.py SCHEMA) and re-run.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
