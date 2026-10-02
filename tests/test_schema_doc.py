"""The overview must expose the complete schema, even with later table sections."""
import re
import runpy
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
DOCUMENT = REPO / "skills/slop-writer/references/schema.md"


def check_document(document, tmp_path):
    script = runpy.run_path(str(REPO / "tools/check_schema_doc.py"))
    path = tmp_path / "schema.md"
    path.write_text(document, encoding="utf-8")
    main = script["main"]
    main.__globals__["SCHEMA_MD"] = path
    return main()


def test_complete_schema_document_matches(tmp_path):
    assert check_document(DOCUMENT.read_text(), tmp_path) == 0


@pytest.mark.parametrize("table", ["posts", "subscriber_bans"])
def test_missing_overview_table_is_rejected_even_when_documented_later(tmp_path, table):
    document = DOCUMENT.read_text()
    pattern = rf"CREATE TABLE {table} \(.*?\);\n"
    incomplete, removed = re.subn(pattern, "", document, count=1, flags=re.DOTALL)
    assert removed == 1
    assert re.search(pattern, incomplete, flags=re.DOTALL)
    assert check_document(incomplete, tmp_path) == 1


def test_missing_sql_blocks_is_rejected(tmp_path):
    assert check_document("# Schema\nNo SQL blocks.\n", tmp_path) == 1
