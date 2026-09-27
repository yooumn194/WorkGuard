"""One-shot evaluator for an externally held, development-blind corpus.

The plaintext dataset must stay outside the repository.  WorkGuard receives
only its SHA-256 before code freeze; the independent evaluator supplies the
file at the final run.  Reports are aggregate-only so examples and labels do
not leak back into the development loop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.db import init_db  # noqa: E402
from eval.extraction_bench import run_mode  # noqa: E402

REPORTS = REPO_ROOT / "eval" / "reports"
DEFAULT_RECEIPTS = REPORTS / "blind_receipts.json"
ALLOWED_PREDICATES = {
    "release_date", "regression_deadline", "gray_release_date", "announcement_date",
}
LLM_USAGE_AGGREGATE_FIELDS = {
    "model", "calls", "errors", "prompt_tokens", "completion_tokens",
    "total_tokens", "estimated_cost_usd", "avg_latency_ms", "by_purpose",
}


class BlindProtocolError(ValueError):
    """The supplied holdout violates the blind-evaluation protocol."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_objects(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        value = json.loads(text)
        return value if isinstance(value, list) else []
    decoder = json.JSONDecoder()
    rows: list[dict] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \n\r\t":
            index += 1
        if index >= len(text):
            break
        row, index = decoder.raw_decode(text, index)
        rows.append(row)
    return rows


def validate_cases(cases: list[dict], minimum_cases: int, minimum_groups: int) -> None:
    if len(cases) < minimum_cases:
        raise BlindProtocolError(
            f"blind dataset needs at least {minimum_cases} cases; received {len(cases)}"
        )
    seen: set[str] = set()
    groups: set[str] = set()
    for position, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            raise BlindProtocolError(f"case {position} must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise BlindProtocolError(f"case {position} has no non-empty id")
        if case_id in seen:
            raise BlindProtocolError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        if not isinstance(case.get("group"), str) or not case["group"].strip():
            raise BlindProtocolError(f"case {case_id} has no group")
        groups.add(case["group"].strip())
        blocks = case.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise BlindProtocolError(f"case {case_id} needs at least one block")
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                raise BlindProtocolError(f"case {case_id} contains an invalid block")
        expected = (case.get("expect") or {}).get("facts")
        if not isinstance(expected, list):
            raise BlindProtocolError(f"case {case_id} needs expect.facts")
        for fact in expected:
            if not isinstance(fact, dict) or not fact.get("predicate") or not fact.get("value"):
                raise BlindProtocolError(f"case {case_id} contains an invalid expected fact")
            if fact["predicate"] not in ALLOWED_PREDICATES:
                raise BlindProtocolError(
                    f"case {case_id} uses unsupported predicate: {fact['predicate']}"
                )
    if len(groups) < minimum_groups:
        raise BlindProtocolError(
            f"blind dataset needs at least {minimum_groups} groups; received {len(groups)}"
        )


def _load_receipts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise BlindProtocolError("blind receipt file must contain a JSON array")
    return value


def _aggregate_llm_usage(usage: dict) -> dict:
    """Drop call-level traces from a blind report while keeping cost totals."""
    return {key: value for key, value in usage.items() if key in LLM_USAGE_AGGREGATE_FIELDS}


def evaluate_blind(
    dataset_path: Path,
    expected_sha256: str,
    mode: str,
    *,
    minimum_cases: int = 30,
    minimum_groups: int = 8,
    report_path: Path | None = None,
    receipt_path: Path = DEFAULT_RECEIPTS,
) -> dict:
    dataset_path = dataset_path.expanduser().resolve()
    if dataset_path.is_relative_to(REPO_ROOT):
        raise BlindProtocolError(
            "blind plaintext must stay outside the WorkGuard repository"
        )
    if not dataset_path.is_file():
        raise BlindProtocolError("blind dataset does not exist or is not a regular file")
    expected_sha256 = expected_sha256.strip().lower()
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise BlindProtocolError("expected SHA-256 must be 64 lowercase hex characters")
    actual_sha256 = sha256_file(dataset_path)
    if actual_sha256 != expected_sha256:
        raise BlindProtocolError("blind dataset SHA-256 does not match the frozen manifest")
    if mode not in {"heuristic", "llm", "hybrid"}:
        raise BlindProtocolError(f"unsupported mode: {mode}")

    receipt_path = receipt_path.expanduser().resolve()
    receipts = _load_receipts(receipt_path)
    if any(r.get("dataset_sha256") == actual_sha256 and r.get("mode") == mode for r in receipts):
        raise BlindProtocolError(
            f"dataset {actual_sha256[:12]} has already been scored in {mode} mode"
        )

    cases = _load_json_objects(dataset_path)
    validate_cases(cases, minimum_cases, minimum_groups)
    # A blind evaluator may point at a brand-new isolated database. Ensure the
    # durable LLM usage schema exists before the one-shot run starts.
    init_db()
    measured = run_mode(mode, cases)
    report = {
        "protocol": "external-one-shot-aggregate-v1",
        "evaluation_status": "valid",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dataset_sha256": actual_sha256,
        "case_count": len(cases),
        "group_count": len({case["group"] for case in cases}),
        "mode": measured["mode"],
        "model": measured["model"],
        "fallback_policy": measured["fallback_policy"],
        "metrics": measured["metrics"],
        "average_latency_ms": measured["avg_extraction_latency_ms"],
        "llm_usage": _aggregate_llm_usage(measured["llm_usage"]),
        "disclosure": "aggregate_only_no_case_ids_text_labels_or_failures",
    }
    report_path = (
        report_path.expanduser().resolve()
        if report_path else REPORTS / f"blind_{actual_sha256[:12]}_{mode}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    receipt_report = (
        str(report_path.relative_to(REPO_ROOT))
        if report_path.is_relative_to(REPO_ROOT) else report_path.name
    )
    receipts.append({
        "dataset_sha256": actual_sha256,
        "mode": mode,
        "evaluation_status": "valid",
        "case_count": len(cases),
        "scored_at": report["generated_at"],
        "report": receipt_report,
    })
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Score an external one-shot blind corpus")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--sha256", required=True, help="hash frozen before the evaluation run")
    parser.add_argument("--mode", choices=["heuristic", "llm", "hybrid"], required=True)
    parser.add_argument("--minimum-cases", type=int, default=30)
    parser.add_argument("--minimum-groups", type=int, default=8)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--receipts", type=Path, default=DEFAULT_RECEIPTS)
    args = parser.parse_args()
    try:
        report = evaluate_blind(
            args.dataset, args.sha256, args.mode,
            minimum_cases=args.minimum_cases,
            minimum_groups=args.minimum_groups,
            report_path=args.report,
            receipt_path=args.receipts,
        )
    except (BlindProtocolError, json.JSONDecodeError) as exc:
        raise SystemExit(f"blind evaluation refused: {exc}") from exc
    summary = {
        key: report[key]
        for key in ("protocol", "evaluation_status", "dataset_sha256", "case_count", "group_count", "mode", "model", "metrics", "average_latency_ms", "disclosure")
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
