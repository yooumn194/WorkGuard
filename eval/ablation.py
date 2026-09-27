"""Ablation experiments over the full pipeline eval (28 cases).

Each configuration disables exactly one defence/mechanism and re-runs the
whole pipeline eval, so every metric delta is attributable:

  baseline                     — everything on
  no_reflection                — Extractor self-check bypassed
  confidence_gate_off          — unverified-lane threshold set to 0 (hedged
                                 facts enter conflict detection)
  no_authority_gates           — truth-transition precondition/decision/
                                 authority checks bypassed
  no_value_scan                — candidate retrieval limited to the fact store
                                 (no old-value text scan over artifacts)
  no_verifier_rules            — historical/context checks removed (rule =
                                 "value differs from old => conflict")
  no_dependency_templates      — impact analysis disabled
  llm_extractor                — extractor switched to the real LLM
                                 (skipped automatically when no key is set)

Usage: .venv/bin/python eval/ablation.py
Output: eval/reports/ablation_report.json + printed delta table.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import backend.agents.extractor as extractor  # noqa: E402
import backend.agents.impact as impact_agent  # noqa: E402
import backend.agents.retriever as retriever  # noqa: E402
import backend.agents.verifier as verifier  # noqa: E402
import backend.config as config_module  # noqa: E402
import backend.llm.client as llm_client_module  # noqa: E402
import backend.tools.fact_store as fact_store  # noqa: E402
import eval.evaluator as pipeline_eval  # noqa: E402
from backend.utils.text import normalize  # noqa: E402
from eval.db_reset import reset_evaluation_database  # noqa: E402

REPORTS = REPO_ROOT / "eval" / "reports"

REPORT_KEYS = [
    "change_detection_accuracy",
    "conflict_precision",
    "conflict_recall",
    "conflict_f1",
    "historical_false_positive_rate",
    "impact_precision",
    "impact_recall",
    "source_attribution_accuracy",
]


def reset_environment() -> None:
    reset_evaluation_database()


# --------------------------------------------------------------- ablation wires
def apply_ablation(name: str) -> list:
    """Patch module attributes; returns a list of undo callables."""
    undos: list = []

    def set_attr(module, attr, value):
        original = getattr(module, attr)
        setattr(module, attr, value)
        undos.append(lambda: setattr(module, attr, original))

    if name == "baseline":
        return undos

    if name == "no_reflection":
        set_attr(extractor, "reflect_facts",
                 lambda blocks, facts, llm=None: (facts, []))

    elif name == "confidence_gate_off":
        set_attr(config_module.settings, "fact_unverified_threshold", 0.0)

    elif name == "no_authority_gates":
        set_attr(fact_store, "assess_truth_transition",
                 lambda session, artifact, current, candidate: (True, "accepted"))

    elif name == "no_value_scan":
        original = retriever.retrieve_candidates

        def fact_store_only(session, workspace_id, change_event):
            return [c for c in original(session, workspace_id, change_event)
                    if c["kind"] == "fact"]

        set_attr(retriever, "retrieve_candidates", fact_store_only)

    elif name == "no_verifier_rules":
        def rule_only(session, change_event, candidates, block_text_lookup=None, llm=None):
            old = normalize(change_event["old_value"])
            results = []
            for candidate in candidates:
                value = normalize(str(candidate.get("value", "")))
                is_old = bool(value) and value == old
                results.append({
                    "artifact_id": candidate["artifact_id"],
                    "artifact_name": candidate.get("artifact_name", candidate["artifact_id"]),
                    "location": candidate.get("location", ""),
                    "fact_id": candidate.get("fact_id"),
                    "evidence": candidate.get("evidence", ""),
                    "kind": candidate.get("kind", "fact"),
                    "verdict": "conflict" if is_old else "need_review",
                    "conflict_type": "outdated_fact" if is_old else "value_diverged",
                    "confidence": 0.9 if is_old else 0.5,
                    "reason": "ablation: value==old => conflict (no historical/context checks)",
                    "recommended_action": "update" if is_old else "review",
                })
            return results

        set_attr(verifier, "verify_candidates", rule_only)

    elif name == "no_dependency_templates":
        set_attr(impact_agent, "analyze_impacts", lambda session, change_event: [])

    elif name == "llm_extractor":
        from backend.config import settings

        if not settings.llm_enabled():
            raise SkipAblation("no LLM key configured")
        # provider already routes extractor through LLMClient; nothing to patch
        return undos

    else:
        raise ValueError(f"unknown ablation: {name}")
    return undos


class SkipAblation(Exception):
    pass


def run_config(name: str, cases: list[dict]) -> dict | None:
    reset_environment()
    original_provider = config_module.settings.llm_provider
    # Causal ablations use the deterministic extractor. The optional LLM row
    # explicitly opts into the model, so a key in .env cannot silently change
    # the baseline underneath the experiment.
    if name == "llm_extractor":
        if not config_module.settings.openai_api_key:
            print("  [llm_extractor] SKIPPED: no LLM key configured")
            return None
        config_module.settings.llm_provider = "openai"
    else:
        config_module.settings.llm_provider = "heuristic"
    llm_client_module._singleton = None
    try:
        undo = apply_ablation(name)
    except SkipAblation as exc:
        print(f"  [{name}] SKIPPED: {exc}")
        return None
    try:
        results = []
        for case in cases:
            results.append(pipeline_eval.run_case(case))
        return pipeline_eval.evaluate(cases, results)
    finally:
        for fn in undo:
            fn()
        config_module.settings.llm_provider = original_provider
        llm_client_module._singleton = None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--include-llm", action="store_true",
        help="also run the real-model row (may incur latency and provider cost)",
    )
    args = parser.parse_args()
    cases = pipeline_eval.load_cases()
    configs = [
        "baseline",
        "no_reflection",
        "confidence_gate_off",
        "no_authority_gates",
        "no_value_scan",
        "no_verifier_rules",
        "no_dependency_templates",
    ]
    if args.include_llm:
        configs.append("llm_extractor")

    print(f"Running {len(cases)} pipeline cases x {len(configs)} ablation configs...\n")
    reports: dict[str, dict] = {}
    for name in configs:
        started = time.perf_counter()
        metrics = run_config(name, cases)
        elapsed = time.perf_counter() - started
        if metrics is None:
            continue
        reports[name] = metrics
        print(f"  {name:24s} f1={metrics['conflict_f1']:.3f} p={metrics['conflict_precision']:.3f} "
              f"r={metrics['conflict_recall']:.3f} hist_fp={metrics['historical_false_positive_rate']:.3f} "
              f"({elapsed:.1f}s)")

    baseline = reports.get("baseline", {})
    print("\n===== Ablation metrics (baseline row = absolute values, others = delta) =====")
    print(f"  {'config':24s} " + " ".join(
        f"{k.replace('conflict_', '').replace('_rate', '').replace('_accuracy', '')[:9]:>9s}"
        for k in REPORT_KEYS))
    for name, metrics in reports.items():
        if name == "baseline":
            print(f"  {'baseline':24s} " + " ".join(f"{metrics[k]:.3f}".rjust(9) for k in REPORT_KEYS))
        else:
            print(f"  {name:24s} " + " ".join(
                f"{metrics[k] - baseline.get(k, 0):+.3f}".rjust(9) for k in REPORT_KEYS))

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / "ablation_report.json"
    out.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "report_keys": REPORT_KEYS,
        "reports": reports,
        "note": "llm_extractor config runs only when an API key is configured; "
                "it switches the extractor (and LLM verifier hooks) to the real model.",
    }, ensure_ascii=False, indent=2))
    print(f"\nreport saved -> {out}")


if __name__ == "__main__":
    main()
