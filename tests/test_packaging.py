"""Guards on what the suite is actually testing.

`tools/check_schema_doc.py` documents the trap these cover: a check that reads
the *published* package passes while the working tree disagrees. The suite has
the same failure mode, and it is invisible — every test would still pass, just
against last release's code.
"""

import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import slop_writer

REPO = Path(__file__).resolve().parent.parent


def test_the_suite_runs_against_the_working_tree():
    """`slop_writer` must resolve to `src/`, not to a wheel from the index."""
    imported = Path(slop_writer.__file__).resolve().parent
    assert imported == REPO / "src" / "slop_writer", (
        f"tests are running against {imported}, not this checkout — "
        "run them with `uv run pytest`, which installs the project editable"
    )


def test_the_library_still_declares_no_cli_dependency():
    """`typer` left the package in 0.2.0 when the domain started raising
    `SlopWriterError` instead of reporting its own failures. A library that
    knows what a CLI is cannot be called by the MCP server."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    deps = " ".join(pyproject["project"]["dependencies"]).lower()
    assert "typer" not in deps
    assert "click" not in deps


def test_dev_dependencies_are_not_project_dependencies():
    """pytest lives in a PEP 735 dependency group, which uv installs for
    development and the build backend never writes into wheel metadata. The
    packaging half of this is asserted in CI, which inspects the built
    wheel — here we only stop it being added to the wrong table."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert "pytest" not in " ".join(pyproject["project"]["dependencies"])
    assert "optional-dependencies" not in pyproject["project"]
    assert "pytest" in " ".join(pyproject["dependency-groups"]["dev"])


def test_the_query_path_stays_importable_without_telethon():
    """`db`, `query` and `errors` are the Telegram-free trio (#22): a caller
    must be able to answer an analytics question without a client.

    Run in a subprocess with Telethon blocked at the import hook, because the
    property is about what the import actually reaches — this suite has
    Telethon loaded from the first factory, so unloading it in-process would
    only prove something about import order. It also holds `__init__.py` to
    its own claim: a star-shaped one would drag Telethon in behind
    `slop_writer.db`.
    """
    program = textwrap.dedent(
        """
        import sys

        class Blocker:
            def find_module(self, name, path=None):
                return self.find_spec(name, path)

            def find_spec(self, name, path=None, target=None):
                if name == "telethon" or name.startswith("telethon."):
                    raise AssertionError(f"reached Telethon via {name}")
                return None

        sys.meta_path.insert(0, Blocker())
        import slop_writer.db, slop_writer.query, slop_writer.errors  # noqa: F401
        print("clean")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "clean"
