"""database package — schema init for phase 1."""

from archetype.db.schema import db_exists_and_ok, init_databases

__all__ = ["init_databases", "db_exists_and_ok"]
