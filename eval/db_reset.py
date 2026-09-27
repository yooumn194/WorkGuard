"""Destructive database reset helpers used only by isolated evaluations."""
from __future__ import annotations

from sqlalchemy import inspect, text

from backend.db import Base, engine, init_db


def reset_evaluation_database() -> None:
    """Recreate the evaluation schema for both Alembic and create-all modes.

    ``Base.metadata.drop_all`` does not know about Alembic's version table. If
    that table survives, a subsequent ``upgrade head`` assumes the application
    tables still exist and leaves an empty database. Evaluation processes use
    a dedicated temporary database, so removing the version marker here is
    safe and makes repeated ablation runs deterministic.
    """
    Base.metadata.drop_all(engine)
    if inspect(engine).has_table("alembic_version"):
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE alembic_version"))
    init_db()
