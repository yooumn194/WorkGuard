"""End-to-end demo of the WorkGuard MVP date-change chain:

    会议修改上线日期 -> 自动发现 PRD/XLSX/MD 冲突 -> 人工批准
      -> 自动修改(MD直写 + DOCX/XLSX建议补丁) -> 校验 -> 回滚

Run:  .venv/bin/python scripts/make_demo_files.py && .venv/bin/python scripts/run_demo.py

Uses the offline heuristic extraction mode (no API key needed); set
OPENAI_API_KEY to switch the extractor/verifier to LLM mode transparently.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_tmp = tempfile.mkdtemp(prefix="workguard_demo_")
os.environ["WORKGUARD_DB_URL"] = f"sqlite:///{_tmp}/demo.db"
os.environ["WORKGUARD_DATA_DIR"] = f"{_tmp}/artifacts"

from sqlalchemy import select  # noqa: E402

from backend.db import SessionLocal, init_db  # noqa: E402
from backend.models import AuditLog  # noqa: E402
from backend.services import changes as change_service  # noqa: E402
from backend.services import chat as chat_service  # noqa: E402
from backend.services import ingest as ingest_service  # noqa: E402

DEMO_DIR = REPO_ROOT / "demo" / "workspace_alpha"
BASELINE = ["PRD.docx", "release_plan.xlsx", "test_plan.docx", "launch_plan.md", "weekly_0829.md"]
TRIGGER = "weekly_0905.md"

PRESET_ENTITIES = [
    {
        "canonical_name": "Alpha V2.0",
        "aliases": ["Alpha", "Alpha V2", "Alpha 2.0", "V2.0"],
        "type": "project_version",
    }
]


def banner(title: str) -> None:
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


def read_launch_plan() -> str:
    artifact_path = None
    with SessionLocal() as session:
        from backend.models import Artifact

        row = session.scalars(
            select(Artifact).where(Artifact.name == "launch_plan.md")
        ).first()
        artifact_path = row.source_path
    return Path(artifact_path).read_text(encoding="utf-8")


def main() -> None:
    init_db()
    with SessionLocal() as session:
        workspace = ingest_service.create_workspace(
            session, "Alpha Workspace", preset_entities=PRESET_ENTITIES
        )
        workspace_id = workspace.id
    print(f"Workspace created: {workspace_id}  (preset entity: Alpha V2.0 + alias dictionary)")

    # ---------------------------------------------------------------- step 1-2
    banner("Step 1  上传基线文档（PRD / XLSX / 测试计划 / 上线计划 / 0829 周会）")
    for filename in BASELINE:
        with SessionLocal() as session:
            artifact = ingest_service.upload_artifact(
                session, workspace_id, filename, (DEMO_DIR / filename).read_bytes()
            )
        result = ingest_service.start_change_detection(workspace_id, artifact.id)
        changes = result["summary"]["change_events"]
        print(f"  uploaded {filename:20s} -> facts extracted, changes detected: {len(changes)}")

    with SessionLocal() as session:
        from backend.models import Fact

        facts = session.scalars(select(Fact).where(Fact.workspace_id == workspace_id)).all()
        verified = [f for f in facts if f.status == "verified"]
        print(f"  Fact Store: {len(facts)} facts ({len(verified)} verified, "
              f"{len(facts) - len(verified)} unverified)")

    # ---------------------------------------------------------------- step 3-4
    banner(f"Step 2  上传触发文档：{TRIGGER}（周会决议：发布时间 09-20 -> 09-27）")
    print(read_launch_plan().strip().splitlines()[3] if False else "")
    with SessionLocal() as session:
        artifact = ingest_service.upload_artifact(
            session, workspace_id, TRIGGER, (DEMO_DIR / TRIGGER).read_bytes()
        )
        artifact_id = artifact.id
    result = ingest_service.start_change_detection(workspace_id, artifact_id)
    events = result["summary"]["change_events"]
    state = result["run_state"]
    if not events:
        print("  !! no change detected — check extraction")
        return
    event_id = events[0]["change_event_id"]
    print(f"  New Change Detected: {event_id}")
    print(f"  {events[0]['entity_name']} / {events[0]['predicate']}")
    print(f"    {events[0]['old_value']}  ->  {events[0]['new_value']}   (confidence {events[0]['confidence']})")
    print(f"  Source: {events[0]['source_artifact_name']}  “{events[0]['evidence']}”")
    print(f"  LangGraph paused at: {state['next']} (waiting human approval)")

    with SessionLocal() as session:
        event = change_service.get_change(session, event_id)
        card = change_service.serialize_change(session, event)
    banner("Step 3  变更分析结果（Change Center）")
    print("  [明确冲突 / Conflicts]")
    for c in card["conflicts"]:
        marker = {"conflict": "✗", "no_conflict": "·", "need_review": "?"}[c["verdict"]]
        print(f"    {marker} {c['artifact']:20s} @ {c['location']:12s} verdict={c['verdict']:11s} "
              f"conf={c['confidence']:.2f}")
        print(f"        原文: “{c['evidence'][:52]}”")
        print(f"        判定: {c['reason'][:66]}")
    print("  [潜在影响 / Impacts]  (Agent 不自动修改)")
    for i in card["impacts"]:
        print(f"    ? {i['artifact']:20s} relation={i['relation'][:40]} type={i['impact_type']}")
        print(f"        {i['reason'][:80]}")
    print("  [修改计划 / Plan]")
    for a in card["plan"]["actions"]:
        print(f"    - {a['artifact']:20s} action={a['action_type']:14s} method={a['method']:16s} "
              f"risk={a['risk']:6s} {a['old_value']} -> {a['new_value']}")

    launch_before = read_launch_plan()

    # ---------------------------------------------------------------- step 5-7
    banner("Step 4  人工批准（POST /changes/{id}/approve  decisions={'all': 'approve'}）")
    outcome = change_service.approve_change(event_id, {"all": "approve"})
    card = outcome["change"]
    print(f"  change status: {card['status']}")
    print("  [执行结果]")
    for a in card["plan"]["actions"]:
        print(f"    - {a['artifact']:20s} {a['action_type']:14s} method={a['method']:16s} "
              f"status={a['status']}")
        if a["method"] == "suggestion" and a["patch_path"]:
            print(f"        建议补丁: {Path(a['patch_path']).name}")
    print("  [Post Verification]")
    for r in outcome["summary"]["post_verification"]:
        status = r.get("result") or ("PASS" if r.get("success") else "FAIL")
        print(f"    - {r['artifact']:20s} {r['method']:16s} -> {status}")

    banner("Step 5  查看自动修改效果（launch_plan.md 直接回写）")

    def date_line(text: str) -> str:
        for line in text.splitlines():
            if "9 月 20 日" in line or "9 月 27 日" in line:
                return line.strip()
        return "(date line not found)"

    print(f"  before: {date_line(launch_before)}")
    launch_after = read_launch_plan()
    print(f"  after : {date_line(launch_after)}")

    banner("Step 6  回滚（POST /changes/{id}/rollback）")
    rolled = change_service.rollback_change(event_id)
    print(f"  change status -> {rolled['change']['status']}")
    for r in rolled["rollbacks"]:
        if "patch_removed" in r:
            print(f"    - action {r['action_id'][:14]}… {r['status']}, suggestion patch removed={r['patch_removed']}")
        else:
            print(f"    - action {r['action_id'][:14]}… {r['status']}, "
                  f"restored from v{r['restored_from_version']} as v{r['current_version']}")
    restored = read_launch_plan()
    same = date_line(restored) == date_line(launch_before)
    print(f"  launch_plan.md restored: {date_line(restored)}")
    assert same, "rollback did not restore the original launch date"

    banner("Step 7  Audit Log（全部动作可追溯）")
    with SessionLocal() as session:
        rows = session.scalars(
            select(AuditLog).where(AuditLog.workspace_id == workspace_id).order_by(AuditLog.executed_at)
        ).all()
        for r in rows:
            print(f"    {r.actor:6s} {r.tool:22s} status={r.status:8s} input={str(r.input)[:60]}")

    banner("Step 8  Chat：基于 Fact Store 的问答（含引用与旧值提示）")
    with SessionLocal() as session:
        answer = chat_service.answer(session, workspace_id, "Alpha V2.0 现在什么时候上线？")
    print("  Q: Alpha V2.0 现在什么时候上线？")
    print("  A: " + answer["answer"].replace("\n", "\n     "))

    print(f"\n(temp demo data in {_tmp} — safe to delete)")
    print("\nDEMO COMPLETE ✓  完整链路：会议改期 -> 冲突发现 -> 人工批准 -> 自动修改 -> 校验 -> 回滚")


if __name__ == "__main__":
    main()
