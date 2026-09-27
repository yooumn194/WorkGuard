import hashlib
import json
from pathlib import Path

import pytest

from eval.blind_evaluator import BlindProtocolError, evaluate_blind


def _write_blind(path: Path) -> str:
    cases = [
        {
            "id": "hidden-a",
            "group": "assertion",
            "blocks": [{"location": "b1", "text": "Alpha 上线日期为 2026-09-27", "meta": {}}],
            "expect": {"facts": [{"predicate": "release_date", "value": "2026-09-27"}]},
        },
        {
            "id": "hidden-b",
            "group": "abstention",
            "blocks": [{"location": "b1", "text": "负责人下周再讨论排期", "meta": {}}],
            "expect": {"facts": []},
        },
    ]
    path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_blind_report_is_aggregate_only_and_one_shot(tmp_path, monkeypatch):
    initialized = []
    monkeypatch.setattr("eval.blind_evaluator.init_db", lambda: initialized.append(True))
    dataset = tmp_path / "held-out.json"
    digest = _write_blind(dataset)
    report_path = tmp_path / "report.json"
    receipts = tmp_path / "receipts.json"

    report = evaluate_blind(
        dataset, digest, "heuristic", minimum_cases=2, minimum_groups=2,
        report_path=report_path, receipt_path=receipts,
    )
    serialized = json.dumps(report, ensure_ascii=False)
    assert report["case_count"] == 2
    assert report["llm_usage"]["calls"] == 0
    assert "records" not in report["llm_usage"]
    assert report["evaluation_status"] == "valid"
    assert initialized == [True]
    assert "per_case" not in report
    assert "hidden-a" not in serialized
    assert "Alpha 上线日期" not in serialized
    assert report_path.exists()

    with pytest.raises(BlindProtocolError, match="already been scored"):
        evaluate_blind(
            dataset, digest, "heuristic", minimum_cases=2, minimum_groups=2,
            report_path=report_path, receipt_path=receipts,
        )


def test_blind_evaluator_rejects_checksum_mismatch(tmp_path):
    dataset = tmp_path / "held-out.json"
    _write_blind(dataset)
    with pytest.raises(BlindProtocolError, match="does not match"):
        evaluate_blind(
            dataset, "0" * 64, "heuristic", minimum_cases=2, minimum_groups=2,
            report_path=tmp_path / "report.json",
            receipt_path=tmp_path / "receipts.json",
        )


def test_blind_evaluator_rejects_repository_plaintext():
    dataset = Path(__file__).resolve().parents[1] / "eval" / "dataset" / "extraction_cases.jsonl"
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    with pytest.raises(BlindProtocolError, match="outside"):
        evaluate_blind(dataset, digest, "heuristic")
