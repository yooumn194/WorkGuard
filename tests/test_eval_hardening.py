import json
import os
import subprocess
import sys
from pathlib import Path

from backend.agents.extractor import extract_facts
from backend.llm.usage import get_usage


class EmptyLLM:
    available = True

    def complete_json(self, system, user, purpose="unspecified"):
        return {"facts": []}


def test_pure_llm_mode_does_not_silently_use_heuristics():
    blocks = [{"location": "line_1", "text": "Alpha V2.0 上线日期 2026-09-27", "meta": {}}]
    assert extract_facts(blocks, EmptyLLM(), allow_fallback=False) == []
    fallback = extract_facts(blocks, EmptyLLM(), allow_fallback=True)
    assert fallback
    assert all(fact["status"] == "unverified" for fact in fallback)
    assert all(fact["confidence"] < 0.6 for fact in fallback)


def test_explicit_offline_mode_never_resolves_default_llm(monkeypatch):
    from backend.agents import extractor

    blocks = [{"location": "line_1", "text": "Alpha 上线日期为 2026-09-27", "meta": {}}]
    monkeypatch.setattr(
        extractor, "get_llm",
        lambda: (_ for _ in ()).throw(AssertionError("default LLM must stay disabled")),
    )
    facts = extractor.extract_facts(blocks, use_default_llm=False)
    kept, _ = extractor.reflect_facts(blocks, facts, use_default_llm=False)
    assert kept


def test_sixty_case_evaluator_can_finish_real_branch_without_fallback(monkeypatch, tmp_path):
    import eval.noisy_evaluator as evaluator

    class EvaluatorLLM(EmptyLLM):
        def metrics(self):
            return get_usage().summary()

    monkeypatch.setattr(evaluator, "LLMClient", EvaluatorLLM)
    monkeypatch.setattr(evaluator, "REPORT", tmp_path / "noisy.json")
    report = evaluator.evaluate(require_llm=True)
    assert len(json.loads(evaluator.DATASET.read_text())) >= 60
    assert report["llm_executed"] is True
    assert report["ablations"]["llm_without_reflection"]["recall"] == 0
    assert evaluator.REPORT.exists()


def test_readme_evaluator_command_works_with_alembic_enabled():
    """Guard the documented entry point, including its Alembic reset path."""
    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env.pop("WORKGUARD_AUTO_MIGRATE", None)
    env["WORKGUARD_LLM_PROVIDER"] = "heuristic"
    result = subprocess.run(
        [sys.executable, "eval/evaluator.py"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Evaluation Report" in result.stdout
