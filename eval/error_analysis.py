"""Error analysis over the extraction benchmark results.

Buckets every failure into a named cause with case evidence, so the next
improvement loop targets the biggest bucket instead of vibes:

  no_parseable_date            — relative/anaphoric expressions ("下周三", "那个时间")
  surface_form_noise           — typos / spacing variants broke keyword or date matching
  english_format               — English month names / phrasing not parsed
  year_inference               — right date, wrong year (cross-year ambiguity)
  hedged_asserted_as_verified  — hedged sentence entered the conflict lane
  suppressed_to_unverified     — correct fact pushed out of the conflict lane
  stale_value_emitted          — old value of an explicit change emitted as current
  predicate_misattribution     — right date attached to the wrong predicate
  conditional_statement_asserted — conditional clause ("若…未完成") asserted as fact
  hallucinated                 — verified fact with no textual basis in ground truth

Usage: .venv/bin/python eval/error_analysis.py [--mode heuristic|llm]
Reads eval/reports/extraction_report_<mode>.json, writes
eval/reports/error_analysis_<mode>.md (+ .json).
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS = REPO_ROOT / "eval" / "reports"
DATASET = REPO_ROOT / "eval" / "dataset" / "extraction_cases.jsonl"

NOISE_GROUPS = {"typo", "spacing", "format"}


def load_cases() -> dict[str, dict]:
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
    return {c["id"]: c for c in cases}


def classify_miss(case: dict, missed: dict) -> str:
    group = case["group"]
    if group == "relative":
        return "no_parseable_date"
    if group == "english":
        return "english_format"
    if group in NOISE_GROUPS:
        return "surface_form_noise"
    text = " ".join(b["text"] for b in case["blocks"])
    value = missed.get("value", "")
    if value and value[:4] not in text and value[5:7].lstrip("0") in text:
        return "year_inference"
    if case["group"] == "hedged":
        return "no_parseable_date"  # anaphoric ("那个时间大概在 9 月 28 日左右")
    return "other"


def classify_spurious(case: dict, spurious: dict, all_expected: list[dict]) -> str:
    text = " ".join(b["text"] for b in case["blocks"])
    value = spurious.get("value", "")
    same_value_expected = any(
        f["value"] == value and f["predicate"] != spurious["predicate"] for f in all_expected
    )
    if same_value_expected:
        return "predicate_misattribution"
    # value appears in text only as the OLD side of a change statement
    if value and value in text and case["group"] in ("change", "crossyear"):
        return "stale_value_emitted"
    if "若" in text or "如果" in text:
        return "conditional_statement_asserted"
    return "hallucinated"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["heuristic", "llm", "hybrid"], default="heuristic")
    args = parser.parse_args()

    report_path = REPORTS / f"extraction_report_{args.mode}.json"
    if not report_path.exists():
        raise SystemExit(f"missing {report_path} — run eval/extraction_bench.py --mode {args.mode} first")
    report = json.loads(report_path.read_text())
    cases = load_cases()

    buckets: Counter = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for case_result in report["per_case"]:
        case = cases[case_result["case_id"]]
        all_expected = case["expect"].get("facts", [])
        for missed in case_result["missed"]:
            cause = classify_miss(case, missed)
            buckets[cause] += 1
            examples[cause].append(
                f"{case['id']}（{case['group']}）漏掉 {missed['predicate']}={missed['value']} ｜ "
                f"原文: {case['blocks'][0]['text'][:42]}"
            )
        for exp in case_result["hedge_miss"]:
            buckets["hedged_asserted_as_verified"] += 1
            examples["hedged_asserted_as_verified"].append(
                f"{case['id']}（{case['group']}）{exp['predicate']}={exp['value']} 被当作已核实事实"
            )
        for exp in case_result["over_suppressed"]:
            buckets["suppressed_to_unverified"] += 1
            examples["suppressed_to_unverified"].append(
                f"{case['id']}（{case['group']}）{exp['predicate']}={exp['value']} 被压到 Unverified 车道"
            )
        for spurious in case_result["spurious"]:
            cause = classify_spurious(case, spurious, all_expected)
            buckets[cause] += 1
            examples[cause].append(
                f"{case['id']}（{case['group']}）多出 {spurious['predicate']}={spurious['value']} ｜ "
                f"原文: {case['blocks'][0]['text'][:42]}"
            )

    lines = [
        f"# 抽取错误分析（{args.mode} 模式）",
        "",
        f"- 数据集：eval/dataset/extraction_cases.jsonl（{report['cases']} 例）",
        f"- 总体：P={report['metrics']['fact_precision']} R={report['metrics']['fact_recall']} "
        f"F1={report['metrics']['fact_f1']}，幻觉(verified)= {report['metrics']['hallucinated_verified_facts']}",
        f"- 模型：{report['model']}；token：{report['llm_usage']['total_tokens']}；"
        f"成本估算：${report['llm_usage']['estimated_cost_usd']}",
        "",
        "## 失败分桶（按数量排序）",
        "",
    ]
    if not buckets:
        lines.append("无失败案例。")
    for cause, count in buckets.most_common():
        lines.append(f"### {cause} × {count}")
        lines.append("")
        for example in examples[cause][:4]:
            lines.append(f"- {example}")
        lines.append("")

    lines += ["## 结论指引", ""]
    if buckets.get("surface_form_noise") or buckets.get("no_parseable_date"):
        lines.append(
            "- 表层噪音/口语指代是主要召回损失来源：启发式靠字面关键词，"
            "LLM 模式预期在此类分组显著占优（有 Key 后运行 extraction_bench --mode llm 验证）。"
        )
    if buckets.get("predicate_misattribution") or buckets.get("stale_value_emitted"):
        lines.append(
            "- 谓词归属/旧值残留是精度损失来源：已通过「变更句整块优先读取」修复一部分，"
            "剩余依赖 Verifier 的历史语境检查兜底。"
        )

    md = "\n".join(lines) + "\n"
    md_out = REPORTS / f"error_analysis_{args.mode}.md"
    md_out.write_text(md)
    (REPORTS / f"error_analysis_{args.mode}.json").write_text(
        json.dumps({"buckets": dict(buckets), "examples": examples}, ensure_ascii=False, indent=2)
    )
    print(md)


if __name__ == "__main__":
    main()
