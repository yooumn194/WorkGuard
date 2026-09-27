"""Verifier rule tests: historical / contextual / conflict / need_review."""
import pytest

from backend.agents.verifier import verify_candidates
from backend.db import SessionLocal, init_db
from backend.models import Workspace
from backend.services.ingest import upload_artifact

EVENT = {
    "change_event_id": "chg_test",
    "workspace_id": "ws_v",
    "entity_id": "ent_alpha",
    "predicate": "release_date",
    "old_value": "2026-09-20",
    "new_value": "2026-09-27",
    "source_artifact_id": "art_source",
}


@pytest.fixture()
def artifacts(tmp_path):
    init_db()
    import docx as docx_lib

    docx_path = tmp_path / "PRD.docx"
    document = docx_lib.Document()
    document.add_paragraph("Alpha V2.0 将于 2026 年 9 月 20 日正式发布。")
    document.save(docx_path)
    meeting_path = tmp_path / "weekly_0829.md"
    meeting_path.write_text("# 0829 周会\n\nAlpha V2.0 计划于 9 月 20 日正式发布。\n")

    with SessionLocal() as session:
        if session.get(Workspace, "ws_v") is None:
            session.add(Workspace(id="ws_v", name="ws_v"))
            session.flush()
        prd = upload_artifact(session, "ws_v", "PRD.docx", docx_path.read_bytes())
        meeting = upload_artifact(session, "ws_v", "weekly_0829.md", meeting_path.read_bytes())
        prd.artifact_role = "document"
        meeting.artifact_role = "meeting"
        session.commit()
        yield {"prd": prd.id, "meeting": meeting.id}


def test_living_document_is_conflict(artifacts):
    candidate = {"kind": "fact", "artifact_id": artifacts["prd"], "location": "para_2",
                 "value": "2026-09-20", "evidence": "Alpha V2.0 将于 9 月 20 日正式发布。"}
    with SessionLocal() as session:
        results = verify_candidates(session, EVENT, [candidate])
    assert results[0]["verdict"] == "conflict"
    assert results[0]["conflict_type"] == "outdated_fact"


def test_meeting_minutes_are_historical_not_conflict(artifacts):
    candidate = {"kind": "fact", "artifact_id": artifacts["meeting"], "location": "line_8",
                 "value": "2026-09-20", "evidence": "Alpha V2.0 计划于 9 月 20 日正式发布。"}
    with SessionLocal() as session:
        results = verify_candidates(session, EVENT, [candidate])
    assert results[0]["verdict"] == "no_conflict"
    assert results[0]["conflict_type"] == "historical_reference"


def test_historical_phrasing_in_living_document(artifacts):
    candidate = {"kind": "text_mention", "artifact_id": artifacts["prd"], "location": "line_4",
                 "value": "2026-09-20", "evidence": "备注：项目原计划 9 月 20 日上线。"}
    with SessionLocal() as session:
        results = verify_candidates(session, EVENT, [candidate])
    assert results[0]["verdict"] == "no_conflict"
    assert results[0]["conflict_type"] == "contextual"


def test_diverged_value_needs_review(artifacts):
    candidate = {"kind": "fact", "artifact_id": artifacts["prd"], "location": "para_1",
                 "value": "2026-09-25", "evidence": "当前口径上线 09-25。"}
    with SessionLocal() as session:
        results = verify_candidates(session, EVENT, [candidate])
    assert results[0]["verdict"] == "need_review"


def test_text_mention_is_conflict(artifacts):
    candidate = {"kind": "text_mention", "artifact_id": artifacts["prd"], "location": "line_2",
                 "value": "2026-09-20", "evidence": "上线日期：09-20"}
    with SessionLocal() as session:
        results = verify_candidates(session, EVENT, [candidate])
    assert results[0]["verdict"] == "conflict"


def test_llm_disagreement_downgrades_conflict_to_review(artifacts):
    """§41 wiring: the LLM re-check can only DOWNGRADE a rule conflict, and the
    divergence is recorded. Rules stay authoritative when the LLM is silent."""
    from backend.agents.verifier import verify_candidates

    class DowngradingLLM:
        available = True

        def complete_json(self, system, user, purpose="unspecified"):
            return {"is_conflict": False, "conflict_type": "contextual",
                    "confidence": 0.8, "reason": "看起来是历史表述",
                    "recommended_action": "review"}

    candidate = {"kind": "fact", "artifact_id": artifacts["prd"], "location": "para_0",
                 "value": "2026-09-20", "evidence": "Alpha V2.0 将于 9 月 20 日正式发布。"}
    with SessionLocal() as session:
        results = verify_candidates(session, EVENT, [candidate], llm=DowngradingLLM())
    assert results[0]["verdict"] == "need_review"
    assert results[0]["llm_review"]["diverged_from_rules"] is True
    assert results[0]["confidence"] < 0.7  # below the write threshold

    class AgreeingLLM(DowngradingLLM):
        def complete_json(self, system, user, purpose="unspecified"):
            return {"is_conflict": True, "conflict_type": "outdated_fact",
                    "confidence": 0.95, "reason": "活文档仍写旧日期",
                    "recommended_action": "update"}

    with SessionLocal() as session:
        results = verify_candidates(session, EVENT, [candidate], llm=AgreeingLLM())
    assert results[0]["verdict"] == "conflict"
    assert results[0]["llm_review"]["diverged_from_rules"] is False

    class SilentLLM:
        available = True

        def complete_json(self, system, user, purpose="unspecified"):
            return None

    with SessionLocal() as session:
        results = verify_candidates(session, EVENT, [candidate], llm=SilentLLM())
    assert results[0]["verdict"] == "conflict"  # unparseable -> rules win
    assert "llm_review" not in results[0]
