"""WorkGuard configuration.

All settings come from environment variables with offline-friendly defaults.
Without an LLM key the whole pipeline runs in deterministic heuristic mode,
which is what the demo and the eval suite exercise by default.

A project-root .env file is loaded automatically (without python-dotenv):
existing environment variables always win over .env values.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _split_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
        return value[1:-1]
    for i, ch in enumerate(value):
        if ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i].strip()  # drop inline comments ("KEY=value  # note")
    return value


def _load_dotenv() -> None:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _split_env_value(raw)


_load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings:
    def __init__(self) -> None:
        self.db_url: str = os.getenv(
            "WORKGUARD_DB_URL", f"sqlite:///{REPO_ROOT / 'data' / 'workguard.db'}"
        )
        self.data_dir: Path = Path(
            os.getenv("WORKGUARD_DATA_DIR", str(REPO_ROOT / "data" / "artifacts"))
        )
        default_checkpoint = self.data_dir.parent / "workguard_checkpoints.db"
        if self.db_url.startswith("sqlite:///"):
            default_checkpoint = Path(self.db_url.removeprefix("sqlite:///"))
            default_checkpoint = default_checkpoint.parent / "workguard_checkpoints.db"
        self.checkpoint_path: Path = Path(
            os.getenv("WORKGUARD_CHECKPOINT_PATH", str(default_checkpoint))
        )
        self.max_upload_bytes: int = _int("WORKGUARD_MAX_UPLOAD_BYTES", 20 * 1024 * 1024)
        self.max_office_uncompressed_bytes: int = _int(
            "WORKGUARD_MAX_OFFICE_UNCOMPRESSED_BYTES", 100 * 1024 * 1024
        )
        self.max_office_archive_entries: int = _int("WORKGUARD_MAX_OFFICE_ARCHIVE_ENTRIES", 10_000)

        # Local development works without a key. Any shared deployment should
        # set one; protected API routes then require X-API-Key or Bearer auth.
        self.api_key: str = os.getenv("WORKGUARD_API_KEY", "").strip()
        self.workspace_auth: bool = _bool("WORKGUARD_WORKSPACE_AUTH", False)
        self.workspace_token_secret: str = os.getenv(
            "WORKGUARD_WORKSPACE_TOKEN_SECRET", self.api_key
        ).strip()
        self.cors_origins: list[str] = _csv(
            "WORKGUARD_CORS_ORIGINS",
            "http://127.0.0.1:8000,http://localhost:8000,"
            "http://127.0.0.1:8765,http://localhost:8765",
        )
        self.auto_migrate: bool = _bool("WORKGUARD_AUTO_MIGRATE", True)
        self.task_worker_enabled: bool = _bool("WORKGUARD_TASK_WORKER", True)
        self.task_poll_seconds: float = _float("WORKGUARD_TASK_POLL_SECONDS", 0.5)
        self.task_lease_seconds: int = _int("WORKGUARD_TASK_LEASE_SECONDS", 900)
        self.task_max_attempts: int = _int("WORKGUARD_TASK_MAX_ATTEMPTS", 3)

        # LLM: "auto" -> OpenAI-compatible API when a key exists, else heuristic mode.
        self.llm_provider: str = os.getenv("WORKGUARD_LLM_PROVIDER", "auto").strip().lower()
        self.llm_model: str = os.getenv("WORKGUARD_LLM_MODEL", "gpt-4o-mini").strip()
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_base_url: str | None = os.getenv("OPENAI_BASE_URL", "").strip() or None

        # Safety thresholds (anti-hallucination layers, see proposal #42).
        self.fact_unverified_threshold: float = _float("WORKGUARD_FACT_UNVERIFIED_THRESHOLD", 0.6)
        self.conflict_review_threshold: float = _float("WORKGUARD_CONFLICT_REVIEW_THRESHOLD", 0.7)
        self.entity_auto_link_threshold: float = _float("WORKGUARD_ENTITY_AUTO_LINK_THRESHOLD", 0.82)

        # Controlled in-place DOCX/XLSX write-back. Default OFF: office files only
        # get suggestion patches so user files are never silently rewritten.
        self.office_write: bool = _bool("WORKGUARD_OFFICE_WRITE", False)

        # Feishu (Lark) integration. Without credentials everything degrades
        # gracefully: the API reports status=not_configured and no calls are made.
        self.feishu_app_id: str = os.getenv("FEISHU_APP_ID", "").strip()
        self.feishu_app_secret: str = os.getenv("FEISHU_APP_SECRET", "").strip()
        self.feishu_webhook_url: str = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
        self.feishu_notify_receive_id: str = os.getenv(
            "FEISHU_NOTIFY_RECEIVE_ID", ""
        ).strip()
        self.feishu_notify_receive_id_type: str = os.getenv(
            "FEISHU_NOTIFY_RECEIVE_ID_TYPE", "chat_id"
        ).strip()
        self.feishu_folder_token: str = os.getenv("FEISHU_FOLDER_TOKEN", "").strip()
        self.feishu_verification_token: str = os.getenv(
            "FEISHU_VERIFICATION_TOKEN", ""
        ).strip()
        self.feishu_encrypt_key: str = os.getenv("FEISHU_ENCRYPT_KEY", "").strip()
        self.feishu_base_url: str = os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn").strip().rstrip("/")
        self.workguard_public_base_url: str = os.getenv(
            "WORKGUARD_PUBLIC_BASE_URL", ""
        ).strip().rstrip("/")

    def feishu_configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret)

    def llm_enabled(self) -> bool:
        if self.llm_provider == "heuristic":
            return False
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        return bool(self.openai_api_key)  # auto


settings = Settings()
