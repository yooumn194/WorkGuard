"""Extraction-level benchmark: heuristic vs real LLM, on the noisy dataset.

Measures FACT-LEVEL quality (not end-to-end): P/R/F1 over (predicate, value)
after Reflection, hedged-sentence routing accuracy, hallucination (verified
fact not grounded in ground truth), entity-attribution accuracy, plus real
token usage from the UsageTracker.

Usage:
    .venv/bin/python eval/extraction_bench.py                 # heuristic
    WORKGUARD_LLM_PROVIDER=openai .venv/bin/python eval/extraction_bench.py --mode llm

Output: eval/reports/extraction_report_<mode>.json + printed table.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import extractor  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.llm.client import LLMClient  # noqa: E402
from backend.llm.usage import get_usage  # noqa: E402

DATASET = REPO_ROOT / "eval" / "dataset" / "extraction_cases.jsonl"
REPORTS = REPO_ROOT / "eval" / "reports"


def load_cases() -> list[dict]:
    decoder = json.JSONDecoder()
    text = DATASET.read_text()
    cases, idx = [], 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \n\r\t":
            idx += 1
        if idx >= len(text):
            break
        obj, idx = decoder.raw_decode(text, idx)
        cases.append(obj)
    return cases


def run_extraction(
    case: dict, llm: LLMClient | None, *, allow_fallback: bool = True
) -> list[dict]:
    """extract + reflect; returns post-reflection facts (dicts)."""
    blocks = case["blocks"]
    use_default_llm = llm is not None
    facts = extractor.extract_facts(
        blocks, llm=llm, allow_fallback=allow_fallback,
        use_default_llm=use_default_llm,
    )
    kept, _ = extractor.reflect_facts(
        blocks, facts, llm=llm, allow_fallback=allow_fallback,
        use_default_llm=use_default_llm,
    )
    return kept


def is_unverified_lane(fact: dict) -> bool:
    return bool(fact.get("uncertain")) or float(fact.get("confidence", 1.0)) <= settings.fact_unverified_threshold


def score_case(case: dict, predicted: list[dict]) -> dict:
    expected = case["expect"].get("facts", [])

    tp, missed, hedge_miss, over_suppressed = 0, [], [], []
    matched_predicted: set[int] = set()
    for exp in expected:
        match = next(
            (
                (i, p)
                for i, p in enumerate(predicted)
                if i not in matched_predicted
                and p["predicate"] == exp["predicate"]
                and p["value"] == exp["value"]
            ),
            None,
        )
        if match is None:
            missed.append(exp)
            continue
        i, pred = match
        matched_predicted.add(i)
        want_unverified = bool(exp.get("unverified"))
        got_unverified = is_unverified_lane(pred)
        if want_unverified and got_unverified:
            tp += 1
        elif want_unverified and not got_unverified:
            hedge_miss.append(exp)  # emitted as verified: would trigger conflicts
        elif not want_unverified and got_unverified:
            over_suppressed.append(exp)  # suppressed below the conflict lane
        else:
            tp += 1

    # verified predictions that ground-truth never allows -> hallucination
    spurious = [
        p for i, p in enumerate(predicted)
        if i not in matched_predicted and not is_unverified_lane(p)
    ]
    extra_unverified = [
        p for i, p in enumerate(predicted)
        if i not in matched_predicted and is_unverified_lane(p)
    ]

    # entity attribution accuracy over matched pairs (secondary metric)
    entity_total = entity_ok = 0
    for exp in expected:
        if not exp.get("entity_mention"):
            continue
        entity_total += 1
        pred = next(
            (p for i, p in enumerate(predicted) if i in matched_predicted
             and p["predicate"] == exp["predicate"] and p["value"] == exp["value"]),
            None,
        )
        if pred and (pred.get("entity_mention") or "").strip() == exp["entity_mention"]:
            entity_ok += 1

    return {
        "case_id": case["id"],
        "group": case["group"],
        "tp": tp,
        "missed": missed,
        "hedge_miss": hedge_miss,
        "over_suppressed": over_suppressed,
        "spurious": spurious,
        "extra_unverified": extra_unverified,
        "entity_ok": entity_ok,
        "entity_total": entity_total,
    }


def aggregate(scores: list[dict]) -> dict:
    tp = sum(s["tp"] for s in scores)
    fp = sum(len(s["spurious"]) for s in scores)
    fn = sum(len(s["missed"]) for s in scores) + sum(len(s["hedge_miss"]) for s in scores) \
        + sum(len(s["over_suppressed"]) for s in scores)
    if tp + fp + fn == 0:
        # perfect abstention: nothing expected, nothing emitted
        precision = recall = f1 = 1.0
    else:
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    entity_total = sum(s["entity_total"] for s in scores)
    return {
        "fact_precision": round(precision, 3),
        "fact_recall": round(recall, 3),
        "fact_f1": round(f1, 3),
        "hedge_routing_errors": sum(len(s["hedge_miss"]) for s in scores),
        "over_suppressed": sum(len(s["over_suppressed"]) for s in scores),
        "hallucinated_verified_facts": fp,
        "entity_attribution_accuracy": round(
            sum(s["entity_ok"] for s in scores) / entity_total, 3
        ) if entity_total else None,
        "confusion": {"tp": tp, "fp": fp, "fn": fn},
    }


def group_breakdown(scores: list[dict]) -> dict:
    by_group: dict[str, list[dict]] = defaultdict(list)
    for s in scores:
        by_group[s["group"]].append(s)
    out = {}
    for group, items in sorted(by_group.items()):
        metrics = aggregate(items)
        out[group] = {k: v for k, v in metrics.items() if k != "confusion"}
        out[group]["cases"] = len(items)
    return out


def run_mode(mode: str, cases: list[dict]) -> dict:
    llm = None
    if mode in ("llm", "hybrid"):
        if not settings.llm_enabled():
            raise SystemExit(
                "LLM mode requested but no API key configured. Set OPENAI_API_KEY "
                "(and OPENAI_BASE_URL for Qwen/DeepSeek compatible endpoints), then rerun."
            )
        llm = LLMClient()
        if not llm.available:
            raise SystemExit("LLM client failed to initialise; check key/base URL.")
    get_usage().reset()

    scores, latencies = [], []
    for case in cases:
        started = time.perf_counter()
        predicted = run_extraction(case, llm, allow_fallback=(mode != "llm"))
        latencies.append((time.perf_counter() - started) * 1000)
        scores.append(score_case(case, predicted))

    return {
        "mode": mode,
        "model": settings.llm_model if mode in ("llm", "hybrid") else "heuristic-regex",
        "fallback_policy": (
            "disabled" if mode == "llm" else "enabled" if mode == "hybrid" else "n/a"
        ),
        "cases": len(cases),
        "metrics": aggregate(scores),
        "per_group": group_breakdown(scores),
        "avg_extraction_latency_ms": round(sum(latencies) / len(latencies), 1),
        "llm_usage": get_usage().summary(),
        "per_case": scores,
    }


def print_report(report: dict) -> None:
    print(f"\n===== Extraction Bench — mode={report['mode']} model={report['model']} =====")
    m = report["metrics"]
    for key in ("fact_precision", "fact_recall", "fact_f1", "hedge_routing_errors",
                "over_suppressed", "hallucinated_verified_facts"):
        print(f"  {key:30s} {m[key]}")
    if m.get("entity_attribution_accuracy") is not None:
        print(f"  {'entity_attribution_accuracy':30s} {m['entity_attribution_accuracy']}")
    print(f"  {'avg_extraction_latency_ms':30s} {report['avg_extraction_latency_ms']}")
    usage = report["llm_usage"]
    print(f"  {'llm_calls':30s} {usage['calls']}")
    print(f"  {'total_tokens':30s} {usage['total_tokens']}")
    print(f"  {'estimated_cost_usd':30s} {usage['estimated_cost_usd']}")
    print("  per group:")
    for group, g in report["per_group"].items():
        print(f"    {group:10s} f1={g['fact_f1']:.3f}  p={g['fact_precision']:.3f}  "
              f"r={g['fact_recall']:.3f}  halluc={g['hallucinated_verified_facts']}  "
              f"hedge_err={g['hedge_routing_errors']}  (cases={g['cases']})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["heuristic", "llm", "hybrid"], default="heuristic")
    args = parser.parse_args()

    cases = load_cases()
    report = run_mode(args.mode, cases)
    print_report(report)

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"extraction_report_{args.mode}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"\nreport saved -> {out}")


if __name__ == "__main__":
    main()
