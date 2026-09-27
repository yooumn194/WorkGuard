"""Database engine / session setup. SQLite by default, PostgreSQL-ready via WORKGUARD_DB_URL."""
from __future__ import annotations

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.config import REPO_ROOT, settings

_connect_args = {"check_same_thread": False} if settings.db_url.startswith("sqlite") else {}
engine = create_engine(settings.db_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from backend import models  # noqa: F401  (register mappings)

    if not settings.auto_migrate:
        Base.metadata.create_all(engine)
        return

    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.db_url.replace("%", "%%"))
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if tables and "alembic_version" not in tables:
        # Adopt databases created by pre-Alembic WorkGuard releases. The first
        # revision is an exact baseline of that schema.
        # The legacy database matches revision 0001. Newer tables must be
        # created by later migrations rather than being silently stamped away.
        baseline_revision = "20260908_0001"
        expected = set(Base.metadata.tables) - {"background_job", "llm_usage_record"}
        missing_tables = expected - tables
        missing_columns = {
            table_name: set(Base.metadata.tables[table_name].columns)
            - {column["name"] for column in inspector.get_columns(table_name)}
            for table_name in expected & tables
        }
        missing_columns = {name: columns for name, columns in missing_columns.items() if columns}
        if missing_tables or missing_columns:
            raise RuntimeError(
                "refusing to stamp a non-baseline database; "
                f"missing_tables={sorted(missing_tables)}, missing_columns={missing_columns}"
            )
        command.stamp(config, baseline_revision)
        command.upgrade(config, "head")
    else:
        command.upgrade(config, "head")


def new_session():
    return SessionLocal()
