"""Telegram channel analytics — the library behind the slop-writer skill.

Nothing is re-exported here on purpose. Import the module you need:

    from slop_writer.db import open_db, data_dir   # stdlib only
    from slop_writer.tg import channel_session     # pulls Telethon
    from slop_writer.render import summarize_scrape
    from slop_writer.markdown import render        # pulls mistune
    from slop_writer.group import classify_service_message

A star-shaped `__init__` would make `import slop_writer.db` drag Telethon and
mistune in behind it, and the Telegram-free half of this package — the schema,
the SQL helpers, the group classification — is exactly what a query tool or a
test wants without a client.

`__version__` is read from the installed distribution metadata rather than
written here, so `pyproject.toml` stays the single place a version is declared.
"""

from importlib.metadata import version

__version__ = version("slop-writer")

__all__ = ["__version__"]
