"""Test bootstrap: isolate env (temp DB/data dir) BEFORE backend imports, and
force heuristic LLM mode so tests are deterministic and offline."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp = tempfile.mkdtemp(prefix="workguard_tests_")
os.environ["WORKGUARD_DB_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["WORKGUARD_DATA_DIR"] = f"{_tmp}/artifacts"
os.environ["WORKGUARD_CHECKPOINT_PATH"] = f"{_tmp}/checkpoints.db"
os.environ["WORKGUARD_AUTO_MIGRATE"] = "0"
os.environ["WORKGUARD_TASK_WORKER"] = "0"
os.environ["WORKGUARD_LLM_PROVIDER"] = "heuristic"
os.environ.pop("OPENAI_API_KEY", None)
