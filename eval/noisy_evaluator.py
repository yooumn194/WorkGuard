"""Noisy Fact Extraction benchmark with optional real-LLM ablations.

Run offline:
    .venv/bin/python eval/noisy_evaluator.py

Require an actual configured OpenAI-compatible model (fails closed otherwise):
    OPENAI_API_KEY=... .venv/bin/python eval/noisy_evaluator.py --require-llm
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.agents import extractor  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.llm.client import LLMClient  # noqa: E402
from backend.llm.usage import get_usage  # noqa: E402

DATASET = ROOT / "eval" / "dataset" / "noisy_date_cases.json"
REPORT = ROOT / "eval" / "reports" / "noisy_report.json"


def _blocks(text: str) -> list[dict]:
    lines = text.splitlines() or [text]
    return [
        {"location": f"line_{index}", "text": line, "meta": {}}
        for index, line in enumerate(lines, 1) if line.strip()
    ]


def _labels(facts: list[dict]) -> set[tuple[str, str, str]]:
    return {
        (
            fact.get("predicate", ""),
            fact.get("value", ""),
            "verified" if float(fact.get("confidence", 0))
            >= settings.fact_unverified_threshold else "unverified",
        )
        for fact in facts
    }


def _score(cases: list[dict], predictions: list[set[tuple]]) -> dict:
    tp = fp = fn = exact = 0
    failures = []
    for case, predicted in zip(cases, predictions):
        expected = {tuple(item) for item in case["expect"]}
        tp += len(predicted & expected)
        fp += len(predicted - expected)
        fn += len(expected - predicted)
        exact += int(predicted == expected)
        if predicted != expected:
            failures.append({
                "id": case["id"], "text": case["text"],
                "expected": sorted(expected), "predicted": sorted(predicted),
            })
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "cases": len(cases), "precision": round(precision, 4),
        "recall": round(recall, 4), "f1": round(f1, 4),
        "exact_case_accuracy": round(exact / len(cases), 4),
        "tp": tp, "fp": fp, "fn": fn, "failure_count": len(failures),
        "failures": failures,
    }


def evaluate(require_llm: bool = False) -> dict:
    get_usage().reset()
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    heuristic_raw, heuristic_reflected = [], []
    for case in cases:
        blocks = _blocks(case["text"])
        raw = extractor._heuristic_extract(blocks)
        kept, _ = extractor._deterministic_review(blocks, raw)
        heuristic_raw.append(_labels(raw))
        heuristic_reflected.append(_labels(kept))

    llm = LLMClient()
    if require_llm and not llm.available:
        raise SystemExit(
            "--require-llm requested, but no working OPENAI_API_KEY/provider is configured"
        )

    report = {
        "dataset": str(DATASET),
        "dataset_kind": "handcrafted noisy office-date corpus",
        "llm_executed": llm.available,
        "provider": settings.llm_provider,
        "model": settings.llm_model if llm.available else None,
        "ablations": {
            "heuristic_without_reflection": _score(cases, heuristic_raw),
            "heuristic_with_reflection": _score(cases, heuristic_reflected),
        },
    }
    if llm.available:
        llm_raw, llm_reflected = [], []
        for case in cases:
            blocks = _blocks(case["text"])
            raw = extractor._llm_extract(blocks, llm)
            kept, _ = extractor.reflect_facts(blocks, raw, llm, allow_fallback=False)
            llm_raw.append(_labels(raw))
            llm_reflected.append(_labels(kept))
        report["ablations"]["llm_without_reflection"] = _score(cases, llm_raw)
        report["ablations"]["llm_with_reflection_and_guards"] = _score(
            cases, llm_reflected
        )
        report["llm_usage"] = llm.metrics()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-llm", action="store_true")
    args = parser.parse_args()
    output = evaluate(args.require_llm)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"\nreport saved -> {REPORT}")
