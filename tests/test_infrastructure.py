"""Infrastructure boundaries that are not exercised by the SQLite API suite."""
from __future__ import annotations


def test_postgres_checkpoint_uses_shared_database_and_runs_setup(monkeypatch):
    from langgraph.checkpoint.postgres import PostgresSaver

    from backend.config import settings
    from backend.graph import workflow

    events: dict[str, object] = {}

    class Saver:
        def setup(self):
            events["setup"] = True

    class Context:
        def __enter__(self):
            events["entered"] = True
            return Saver()

        def __exit__(self, *args):
            events["exited"] = True

    def from_conn_string(url):
        events["url"] = url
        return Context()

    monkeypatch.setattr(settings, "db_url", "postgresql+psycopg://user:pass@db/workguard")
    monkeypatch.setattr(PostgresSaver, "from_conn_string", from_conn_string)
    workflow._checkpoint_context = None
    workflow._compiled = None

    saver = workflow.get_checkpointer()
    assert isinstance(saver, Saver)
    assert events == {
        "url": "postgresql://user:pass@db/workguard",
        "entered": True,
        "setup": True,
    }
    workflow.close_checkpointer()
    assert events["exited"] is True
