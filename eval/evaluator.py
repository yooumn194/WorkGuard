"""OfficeConsistencyBench evaluator (date-change subset, MVP).

Runs the full pipeline (heuristic mode by default) over eval/dataset/cases.jsonl
and reports REAL measured numbers — Conflict Precision / Recall / F1, Change
Detection Accuracy, Historical False-Positive Rate, Entity Resolution Accuracy,
Source Attribution and Impact Precision/Recall. A mixed-format execution probe
also measures approval safety, tool/patch/post-verify success and latency.

Run: .venv/bin/python eval/evaluator.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_tmp = tempfile.mkdtemp(prefix="workguard_eval_")
os.environ["WORKGUARD_DB_URL"] = f"sqlite:///{_tmp}/eval.db"
os.environ["WORKGUARD_DATA_DIR"] = f"{_tmp}/artifacts"

from sqlalchemy import select  # noqa: E402

from backend.config import settings  # noqa: E402
from backend.db import SessionLocal  # noqa: E402
from backend.models import (  # noqa: E402
    Artifact,
    ChangeEvent,
    Conflict,
    Entity,
    Fact,
    Impact,
)
from backend.services import changes as change_service  # noqa: E402
from backend.services import ingest as ingest_service  # noqa: E402
from eval.db_reset import reset_evaluation_database  # noqa: E402

DATASET = REPO_ROOT / "eval" / "dataset" / "cases.jsonl"
REPORTS = REPO_ROOT / "eval" / "reports"


def load_cases() -> list[dict]:
    """Dataset is newline-separated pretty-printed JSON objects; decode with a
    streaming raw_decode loop instead of strict JSONL."""
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


def run_case(case: dict) -> dict:
    """Returns per-case actual results."""
    with SessionLocal() as session:
        workspace = ingest_service.create_workspace(
            session, f"eval-{case['id']}", preset_entities=case.get("preset_entities")
        )
        workspace_id = workspace.id

    detected_change = False
    conflicts, no_conflicts, need_review, sources, impacts = [], [], [], [], []

    for doc in case["docs"]:
        with SessionLocal() as session:
            artifact = ingest_service.upload_artifact(
                session, workspace_id, doc["name"], doc["content"].encode("utf-8")
            )
        result = ingest_service.start_change_detection(workspace_id, artifact.id)
        if result["summary"]["change_events"]:
            detected_change = True

    with SessionLocal() as session:
        events = session.scalars(
            select(ChangeEvent).where(ChangeEvent.workspace_id == workspace_id)
        ).all()
        detected_change = len(events) > 0
        for event in events:
            source = session.get(Artifact, event.source_artifact_id)
            if source:
                sources.append(source.name)
            rows = session.scalars(
                select(Conflict).where(Conflict.change_event_id == event.id)
            ).all()
            for row in rows:
                artifact = session.get(__import__("backend.models", fromlist=["Artifact"]).Artifact, row.artifact_id)
                if row.verdict == "conflict":
                    conflicts.append(artifact.name)
                elif row.verdict == "need_review":
                    need_review.append(artifact.name)
                else:
                    no_conflicts.append(artifact.name)
            for impact in session.scalars(
                select(Impact).where(Impact.change_event_id == event.id)
            ).all():
                impacts.append(impact.relation.split(" ", 1)[0])

        # entity resolution accuracy: verified facts must carry the right entity
        facts = session.scalars(select(Fact).where(Fact.workspace_id == workspace_id)).all()
        expected_names = {p["canonical_name"] for p in case.get("preset_entities", [])}
        entity_ok = entity_total = 0
        for fact in facts:
            if fact.status != "verified":
                continue
            entity_total += 1
            entity = session.get(Entity, fact.entity_id) if fact.entity_id else None
            if entity and entity.canonical_name in expected_names:
                entity_ok += 1

    return {
        "case_id": case["id"],
        "detected_change": detected_change,
        "conflicts": sorted(set(conflicts)),
        "need_review": sorted(set(need_review)),
        "no_conflicts": sorted(set(no_conflicts)),
        "sources": sorted(set(sources)),
        "impacts": sorted(set(impacts)),
        "entity_ok": entity_ok,
        "entity_total": entity_total,
    }


def evaluate(cases: list[dict], results: list[dict], execution: dict | None = None) -> dict:
    tp = fp = fn = 0
    change_correct = 0
    historical_fp = historical_total = 0
    source_correct = source_total = 0
    impact_tp = impact_fp = impact_fn = 0
    entity_ok = entity_total = 0

    for case, actual in zip(cases, results):
        expected_conflicts = set(case["expect"].get("conflicts", []))
        predicted = set(actual["conflicts"])
        tp += len(expected_conflicts & predicted)
        fp += len(predicted - expected_conflicts)
        fn += len(expected_conflicts - predicted)
        change_correct += int(case["expect"]["change_detected"] == actual["detected_change"])
        safe_refs = set(case["expect"].get("no_conflicts", []))
        historical_fp += len(safe_refs & predicted)
        historical_total += len(safe_refs)
        if case["expect"]["change_detected"]:
            source_total += 1
            expected_source = case["expect"].get("source_artifact", case["docs"][-1]["name"])
            source_correct += int(expected_source in actual["sources"])
        expected_impacts = set(case["expect"].get("impacts", []))
        predicted_impacts = set(actual["impacts"])
        impact_tp += len(expected_impacts & predicted_impacts)
        impact_fp += len(predicted_impacts - expected_impacts)
        impact_fn += len(expected_impacts - predicted_impacts)
        entity_ok += actual["entity_ok"]
        entity_total += actual["entity_total"]

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    report = {
        "cases": len(cases),
        "change_detection_accuracy": change_correct / len(cases),
        "conflict_precision": precision,
        "conflict_recall": recall,
        "conflict_f1": f1,
        "historical_false_positive_rate": historical_fp / historical_total if historical_total else 0.0,
        "entity_resolution_accuracy": (entity_ok / entity_total) if entity_total else 1.0,
        "source_attribution_accuracy": source_correct / source_total if source_total else 1.0,
        "impact_precision": impact_tp / (impact_tp + impact_fp) if impact_tp + impact_fp else 1.0,
        "impact_recall": impact_tp / (impact_tp + impact_fn) if impact_tp + impact_fn else 1.0,
        "confusion": {"tp": tp, "fp": fp, "fn": fn},
    }
    report.update(execution or {})
    return report


def run_execution_benchmark() -> dict:
    """Measure approval safety and tool outcomes on the real mixed-format demo."""
    usage_summary = _usage_summary()
    demo = REPO_ROOT / "demo" / "workspace_alpha"
    presets = [{"canonical_name": "Alpha V2.0", "aliases": ["Alpha", "V2.0"]}]
    with SessionLocal() as session:
        workspace = ingest_service.create_workspace(session, "eval-execution", presets)
        workspace_id = workspace.id

    for name in ["PRD.docx", "release_plan.xlsx", "test_plan.docx",
                 "launch_plan.md", "weekly_0829.md"]:
        with SessionLocal() as session:
            artifact = ingest_service.upload_artifact(
                session, workspace_id, name, (demo / name).read_bytes()
            )
        ingest_service.start_change_detection(workspace_id, artifact.id)

    with SessionLocal() as session:
        trigger = ingest_service.upload_artifact(
            session, workspace_id, "weekly_0905.md", (demo / "weekly_0905.md").read_bytes()
        )
        artifacts = session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id)).all()
        before = {
            artifact.id: hashlib.sha256(Path(artifact.source_path).read_bytes()).hexdigest()
            for artifact in artifacts
        }
    started = time.perf_counter()
    result = ingest_service.start_change_detection(workspace_id, trigger.id)
    change_id = result["summary"]["change_events"][0]["change_event_id"]

    with SessionLocal() as session:
        event = session.get(ChangeEvent, change_id)
        planned_actions = [a for a in event.plan.actions if a.action_type != "human_review"]
    unsafe_writes = 0
    with SessionLocal() as session:
        for artifact_id, digest in before.items():
            artifact = session.get(Artifact, artifact_id)
            unsafe_writes += int(
                hashlib.sha256(Path(artifact.source_path).read_bytes()).hexdigest() != digest
            )

    outcome = change_service.approve_change(change_id, {"all": "approve"})
    elapsed_ms = (time.perf_counter() - started) * 1000
    actions = [a for a in outcome["change"]["plan"]["actions"] if a["action_type"] != "human_review"]
    reports = outcome["summary"]["post_verification"]
    patches = [a for a in actions if a["method"] == "suggestion"]

    return {
        "unsafe_update_rate": unsafe_writes / len(planned_actions) if planned_actions else 0.0,
        "tool_success_rate": sum(a["status"] == "executed" for a in actions) / len(actions),
        "patch_success_rate": (
            sum(a["status"] == "executed" and bool(a["patch_path"]) for a in patches) / len(patches)
            if patches else 1.0
        ),
        "post_verify_success_rate": (
            sum(bool(r.get("success")) for r in reports) / len(reports) if reports else 1.0
        ),
        "execution_latency_ms": round(elapsed_ms, 3),
        "llm_calls": usage_summary["calls"],
        "token_usage_total": usage_summary["total_tokens"],
        "estimated_token_cost_usd": usage_summary["estimated_cost_usd"],
        "llm_usage": usage_summary,
    }


def _usage_summary() -> dict:
    from backend.llm.usage import get_usage

    return get_usage().summary()


def main() -> None:
    reset_evaluation_database()
    from backend.llm.usage import get_usage

    get_usage().reset()  # one eval run = one clean accounting window
    cases = load_cases()
    results = []
    case_latencies = []
    mode = "hybrid" if settings.llm_enabled() else "heuristic"
    print(f"Running {len(cases)} cases ({mode} mode)...\n")
    for case in cases:
        started = time.perf_counter()
        actual = run_case(case)
        case_latencies.append((time.perf_counter() - started) * 1000)
        results.append(actual)
        expected_conflicts = case["expect"].get("conflicts", [])
        status = "OK" if set(expected_conflicts) == set(actual["conflicts"]) else "MISMATCH"
        print(f"  [{status:8s}] {case['id']}  expected={expected_conflicts}  "
              f"got conflicts={actual['conflicts']} need_review={actual['need_review']} "
              f"no_conflict={actual['no_conflicts']}")

    execution = run_execution_benchmark()
    execution["avg_case_latency_ms"] = round(sum(case_latencies) / len(case_latencies), 3)
    report = evaluate(cases, results, execution)
    print("\n================ Evaluation Report ================")
    for key, value in report.items():
        if key == "confusion":
            continue
        print(f"  {key:34s} {value:.3f}" if isinstance(value, float) else f"  {key:34s} {value}")
    print(f"  confusion (tp/fp/fn)               {report['confusion']}")

    REPORTS.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "provider": settings.llm_provider if mode == "hybrid" else "heuristic",
        "model": settings.llm_model if mode == "hybrid" else "heuristic-regex",
        "fallback_policy": "enabled" if mode == "hybrid" else "n/a",
        "metrics": report,
        "per_case": [
            {"case_id": c["id"], **{k: v for k, v in a.items() if k != "case_id"}}
            for c, a in zip(cases, results)
        ],
    }
    out = REPORTS / "report.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\nreport saved -> {out}")


if __name__ == "__main__":
    main()
