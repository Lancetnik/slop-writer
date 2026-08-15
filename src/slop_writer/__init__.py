"""Telegram channel analytics.

This release exports nothing on purpose. It exists to prove the packaging and
publishing loop end to end — build, trusted-publish, resolve from PyPI — while
that loop is the only variable. The domain modules (`db`, `tg`, `render`,
`markdown`, `group`) move here next, once the loop is known to work.

`__version__` is read from the installed distribution metadata rather than
written here, so `pyproject.toml` stays the single place a version is declared.
"""

from importlib.metadata import version

__version__ = version("slop-writer")

__all__ = ["__version__"]
