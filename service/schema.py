"""
Canonical application DDL.

The `.sql` files under `service/sql/` are the single definition of the `chat`
and `auth` schemas. They are applied by two paths, and both read these same
files — there is no second copy to drift from:

  * **fresh database** — the files are mounted into the container's
    `docker-entrypoint-initdb.d` (docker-compose.yml), or applied by
    `scripts/bootstrap-db.sh` for a managed instance such as Cloud SQL
  * **existing database** — `ensure_schema()` on each store re-applies them at
    every service startup, which is the migration path for volumes that predate
    a schema change

Everything in these files must therefore be idempotent and safe to re-run
against a live database under load.

They ship inside the installed package (`[tool.setuptools.package-data]`), so
`pip install .` and the Cloud image carry them; `tests/test_schema.py` guards
that they remain loadable and enforce what they claim.
"""

from __future__ import annotations

from importlib import resources

CHAT_SCHEMA = "04-chat-schema.sql"
AUTH_SCHEMA = "05-auth-schema.sql"

#: In apply order. 05 adds a foreign key into the table 04 creates.
ALL_SCHEMAS = (CHAT_SCHEMA, AUTH_SCHEMA)


def load(name: str) -> str:
    """Read one canonical schema file out of the installed package.

    `importlib.resources` rather than a path relative to `__file__`, so this
    works from a wheel, a zipapp, or the source tree alike.
    """
    return (resources.files("service") / "sql" / name).read_text(encoding="utf-8")
