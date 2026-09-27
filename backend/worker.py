"""Standalone durable-job worker entry point used by Docker Compose."""
from __future__ import annotations

from backend.db import init_db
from backend.services.jobs import run_worker_forever


def main() -> None:
    init_db()
    run_worker_forever()


if __name__ == "__main__":
    main()
