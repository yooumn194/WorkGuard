"""HTTP contract tests for the complete date-change lifecycle."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.db import Base, SessionLocal, engine, init_db
from backend.main import app


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()


def _workspace(client: TestClient, name: str = "api-test") -> str:
    response = client.post("/api/workspaces", json={
        "name": name,
        "preset_entities": [{"canonical_name": "Alpha V2.0", "aliases": ["Alpha"]}],
    })
    assert response.status_code == 200
    return response.json()["workspace_id"]


def _upload(client: TestClient, workspace_id: str, name: str, text: str):
    return client.post(
        f"/api/workspaces/{workspace_id}/artifacts?sync=true",
        files={"file": (name, text.encode("utf-8"), "text/markdown")},
    )


def test_health_and_change_center_assets_are_served():
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        root = client.get("/", follow_redirects=False)
        assert root.status_code in (302, 307)
        assert root.headers["location"] == "/app/"

        page = client.get("/app/")
        assert page.status_code == 200
        assert "Change Center" in page.text
        assert "载入演示数据" in page.text

        assert client.get("/app/styles.css").status_code == 200
        assert client.get("/app/app.js").status_code == 200
        assert client.get("/demo-assets/weekly_0905.md").status_code == 200


def test_http_date_change_approve_query_and_idempotent_rollback():
    with TestClient(app) as client:
        workspace_id = _workspace(client)
        baseline = _upload(
            client, workspace_id, "launch_plan.md",
            "# Alpha V2.0\n上线日期：2026-09-20。\n",
        )
        assert baseline.status_code == 201
        artifact_id = baseline.json()["artifact_id"]
        assert baseline.json()["summary"]["change_events"] == []

        decision = _upload(
            client, workspace_id, "weekly_decision.md",
            "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n",
        )
        assert decision.status_code == 201
        payload = decision.json()
        assert payload["run_state"]["waiting_approval"] is True
        change_id = payload["summary"]["change_events"][0]["change_event_id"]

        artifacts = client.get(f"/api/workspaces/{workspace_id}/artifacts").json()
        assert len(artifacts) == 2
        assert all("source_authority" in artifact for artifact in artifacts)
        assert client.get(f"/api/artifacts/{artifact_id}").status_code == 200
        assert client.get(f"/api/workspaces/{workspace_id}/facts?current_only=true").status_code == 200
        assert client.get(f"/api/workspaces/{workspace_id}/changes").json()[0]["change_id"] == change_id
        card = client.get(f"/api/changes/{change_id}")
        assert card.status_code == 200
        assert card.json()["status"] == "pending_approval"
        assert client.get(f"/api/runs/{payload['thread_id']}").json()["waiting_approval"] is True

        approved = client.post(
            f"/api/changes/{change_id}/approve",
            json={"decisions": {"all": "approve"}},
        )
        assert approved.status_code == 200
        assert approved.json()["change"]["status"] == "executed"
        executed_card = client.get(f"/api/changes/{change_id}").json()
        executed_launch = next(
            action for action in executed_card["plan"]["actions"]
            if action["artifact"] == "launch_plan.md"
        )
        assert "2026-09-20" in executed_launch["before_text"]
        assert "2026-09-27" in executed_launch["after_text"]
        assert executed_launch["before_text"] != executed_launch["after_text"]
        blocks = client.get(f"/api/artifacts/{artifact_id}").json()["blocks"]
        assert any("2026-09-27" in block["text"] for block in blocks)
        assert client.post(f"/api/changes/{change_id}/approve").status_code == 409

        rolled = client.post(f"/api/changes/{change_id}/rollback")
        assert rolled.status_code == 200
        assert rolled.json()["change"]["status"] == "rolled_back"
        repeated = client.post(f"/api/changes/{change_id}/rollback")
        assert repeated.status_code == 200
        assert repeated.json()["already_rolled_back"] is True


def test_http_reject_and_validation_errors():
    with TestClient(app) as client:
        workspace_id = _workspace(client, "reject-test")
        assert _upload(client, workspace_id, "plan.md",
                       "Alpha V2.0 上线日期：2026-09-20。\n").status_code == 201
        detected = _upload(
            client, workspace_id, "meeting.md",
            "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n",
        ).json()
        change_id = detected["summary"]["change_events"][0]["change_event_id"]

        bad_value = client.post(
            f"/api/changes/{change_id}/approve",
            json={"decisions": {"all": "maybe"}},
        )
        assert bad_value.status_code == 409
        unknown = client.post(
            f"/api/changes/{change_id}/approve",
            json={"decisions": {"act_missing": "approve"}},
        )
        assert unknown.status_code == 409
        rejected = client.post(f"/api/changes/{change_id}/reject")
        assert rejected.status_code == 200
        assert rejected.json()["change"]["status"] == "rejected"
        assert client.post(f"/api/changes/{change_id}/reject").status_code == 409

        unsupported = client.post(
            f"/api/workspaces/{workspace_id}/artifacts?sync=true",
            files={"file": ("notes.pdf", b"not a pdf", "application/pdf")},
        )
        assert unsupported.status_code == 400
        missing_workspace = _upload(
            client, "ws_missing", "plan.md", "Alpha V2.0 上线日期：2026-09-20。\n"
        )
        assert missing_workspace.status_code == 400


def test_http_partial_approval_is_fail_closed():
    with TestClient(app) as client:
        workspace_id = _workspace(client, "partial-api")
        for name in ("plan_a.md", "plan_b.md"):
            assert _upload(
                client, workspace_id, name,
                "Alpha V2.0 上线日期：2026-09-20。\n",
            ).status_code == 201
        detected = _upload(
            client, workspace_id, "meeting.md",
            "Alpha V2.0 上线日期由 2026-09-20 调整至 2026-09-27。\n",
        ).json()
        change_id = detected["summary"]["change_events"][0]["change_event_id"]
        card = client.get(f"/api/changes/{change_id}").json()
        direct = {a["artifact"]: a["action_id"] for a in card["plan"]["actions"]
                  if a["method"] == "direct_write"}

        response = client.post(
            f"/api/changes/{change_id}/approve",
            json={"decisions": {direct["plan_a.md"]: "approve"}},
        )
        assert response.status_code == 200
        body = response.json()["change"]
        assert body["status"] == "partially_executed"
        statuses = {a["artifact"]: a["status"] for a in body["plan"]["actions"]
                    if a["method"] == "direct_write"}
        assert statuses == {"plan_a.md": "executed", "plan_b.md": "skipped"}


def test_http_rejects_corrupt_office_empty_and_oversized_uploads(monkeypatch):
    with TestClient(app) as client:
        workspace_id = _workspace(client, "upload-guards")
        for name in ("broken.docx", "broken.xlsx"):
            response = client.post(
                f"/api/workspaces/{workspace_id}/artifacts?sync=true",
                files={"file": (name, b"not-an-office-zip", "application/octet-stream")},
            )
            assert response.status_code == 400
            assert "valid Office" in response.json()["detail"]

        empty = client.post(
            f"/api/workspaces/{workspace_id}/artifacts?sync=true",
            files={"file": ("empty.md", b"", "text/markdown")},
        )
        assert empty.status_code == 400

        monkeypatch.setattr(settings, "max_upload_bytes", 8)
        oversized = _upload(client, workspace_id, "large.md", "123456789")
        assert oversized.status_code == 413
        assert "too large" in oversized.json()["detail"]


def test_api_key_protects_api_but_not_health_or_feishu_callback(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "interview-secret")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/api/workspaces", json={"name": "blocked"}).status_code == 401
        authorized = client.post(
            "/api/workspaces",
            headers={"Authorization": "Bearer interview-secret"},
            json={"name": "authorized"},
        )
        assert authorized.status_code == 200

        # Feishu performs its own verification-token validation and cannot add
        # the WorkGuard API key header.
        callback = client.post(
            "/api/integrations/feishu/webhook",
            json={"type": "url_verification", "challenge": "hello"},
        )
        assert callback.status_code != 401


def test_local_mode_rejects_cross_site_state_changes(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    with TestClient(app) as client:
        blocked = client.post(
            "/api/workspaces",
            headers={"Origin": "https://attacker.example"},
            json={"name": "cross-site"},
        )
        assert blocked.status_code == 403
        allowed = client.post(
            "/api/workspaces",
            headers={"Origin": "http://127.0.0.1:8000"},
            json={"name": "same-site"},
        )
        assert allowed.status_code == 200
        app_origin = client.post(
            "/api/workspaces",
            headers={"Origin": "http://127.0.0.1:8765"},
            json={"name": "served-app-origin"},
        )
        assert app_origin.status_code == 200


def test_cors_preflight_allows_workspace_capability_header():
    with TestClient(app) as client:
        response = client.options(
            "/api/workspaces/example/artifacts",
            headers={
                "Origin": "http://127.0.0.1:8000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Workspace-Key",
            },
        )
        assert response.status_code == 200
        assert "x-workspace-key" in response.headers[
            "access-control-allow-headers"
        ].lower()


def test_workspace_tokens_prevent_cross_workspace_and_indirect_id_access(monkeypatch):
    monkeypatch.setattr(settings, "workspace_auth", True)
    monkeypatch.setattr(settings, "workspace_token_secret", "workspace-signing-secret")
    with TestClient(app) as client:
        first = client.post("/api/workspaces", json={"name": "first"}).json()
        second = client.post("/api/workspaces", json={"name": "second"}).json()
        first_headers = {"X-Workspace-Key": first["workspace_key"]}
        second_headers = {"X-Workspace-Key": second["workspace_key"]}

        assert client.get(
            f"/api/workspaces/{first['workspace_id']}/artifacts", headers=first_headers
        ).status_code == 200
        assert client.get(
            f"/api/workspaces/{second['workspace_id']}/artifacts", headers=first_headers
        ).status_code == 403

        uploaded = client.post(
            f"/api/workspaces/{first['workspace_id']}/artifacts?sync=true",
            headers=first_headers,
            files={"file": ("plan.md", b"Alpha release date: 2026-09-20", "text/markdown")},
        )
        assert uploaded.status_code == 201
        artifact_id = uploaded.json()["artifact_id"]
        assert client.get(f"/api/artifacts/{artifact_id}", headers=second_headers).status_code == 403
        assert client.get(f"/api/artifacts/{artifact_id}", headers=first_headers).status_code == 200

        queued = client.post(
            f"/api/workspaces/{first['workspace_id']}/artifacts",
            headers=first_headers,
            files={"file": ("queued.md", b"Alpha release date: 2026-09-27", "text/markdown")},
        )
        assert queued.status_code == 202
        job_id = queued.json()["job_id"]
        assert client.get(f"/api/jobs/{job_id}", headers=second_headers).status_code == 403
        assert client.get(f"/api/jobs/{job_id}", headers=first_headers).status_code == 200


def test_audit_payloads_are_hidden_by_default_and_redacted_when_requested():
    from backend.tools.audit import log_action

    with TestClient(app) as client:
        workspace_id = _workspace(client, "redacted-audit")
        with SessionLocal() as session:
            log_action(
                session,
                workspace_id,
                "credential_test",
                {"api_key": "must-not-leak", "nested": {"password": "hidden", "safe": "shown"}},
                {"access_token": "must-not-leak", "status": "ok"},
            )
            session.commit()

        default_row = client.get(f"/api/workspaces/{workspace_id}/audit").json()[0]
        assert default_row["input"] == {}
        assert default_row["output"] == {}
        assert default_row["has_payload"] is True

        detailed = client.get(
            f"/api/workspaces/{workspace_id}/audit?include_payloads=true"
        ).json()[0]
        assert detailed["input"]["api_key"] == "[REDACTED]"
        assert detailed["input"]["nested"]["password"] == "[REDACTED]"
        assert detailed["input"]["nested"]["safe"] == "shown"
        assert detailed["output"]["access_token"] == "[REDACTED]"


def test_http_rejects_office_archive_entry_and_expansion_limits(monkeypatch):
    office = (
        Path(__file__).resolve().parent.parent
        / "demo" / "workspace_alpha" / "PRD.docx"
    ).read_bytes()
    with TestClient(app) as client:
        workspace_id = _workspace(client, "office-archive-guards")

        original_entries = settings.max_office_archive_entries
        monkeypatch.setattr(settings, "max_office_archive_entries", 1)
        too_many = client.post(
            f"/api/workspaces/{workspace_id}/artifacts?sync=true",
            files={"file": ("PRD.docx", office, "application/octet-stream")},
        )
        assert too_many.status_code == 400
        assert "too many archive entries" in too_many.json()["detail"]

        monkeypatch.setattr(settings, "max_office_archive_entries", original_entries)
        monkeypatch.setattr(settings, "max_office_uncompressed_bytes", 1)
        expanded = client.post(
            f"/api/workspaces/{workspace_id}/artifacts?sync=true",
            files={"file": ("PRD.docx", office, "application/octet-stream")},
        )
        assert expanded.status_code == 400
        assert "expanded Office file too large" in expanded.json()["detail"]


def test_http_per_action_approval_with_diff_previews():
    """UI closed loop: per-action decisions apply exactly, and every
    modifiable action carries before/after diff previews."""
    demo = Path(__file__).resolve().parent.parent / "demo" / "workspace_alpha"
    with TestClient(app) as client:
        workspace_id = _workspace(client, "per-action-ui")
        for name in ["PRD.docx", "release_plan.xlsx", "launch_plan.md", "weekly_0829.md"]:
            uploaded = client.post(
                f"/api/workspaces/{workspace_id}/artifacts?sync=true",
                files={"file": (name, demo.joinpath(name).read_bytes())},
            )
            assert uploaded.status_code in (200, 201, 202)
        client.post(
            f"/api/workspaces/{workspace_id}/artifacts?sync=true",
            files={"file": ("weekly_0905.md", demo.joinpath("weekly_0905.md").read_bytes())},
        )
        changes = client.get(f"/api/workspaces/{workspace_id}/changes").json()
        change_id = changes[0]["change_id"]
        card = client.get(f"/api/changes/{change_id}").json()

        modifiable = [a for a in card["plan"]["actions"] if a["action_type"] != "human_review"]
        assert modifiable
        launch = next(a for a in modifiable if a["artifact"] == "launch_plan.md")
        assert "9 月 27 日" in launch["after_text"]
        assert "9 月 20 日" in launch["before_text"]
        assert launch["before_text"] != launch["after_text"]

        decisions = {
            a["action_id"]: ("approve" if a["artifact"] == "launch_plan.md" else "reject")
            for a in modifiable
        }
        approved = client.post(
            f"/api/changes/{change_id}/approve",
            json={"decisions": decisions},
        )
        assert approved.status_code == 200
        rows = {(a["artifact"], a["action_type"]): a["status"]
                for a in approved.json()["change"]["plan"]["actions"]}
        assert rows[("launch_plan.md", "update_artifact")] == "executed"
        assert rows[("PRD.docx", "suggest_patch")] == "skipped"
        assert rows[("release_plan.xlsx", "suggest_patch")] == "skipped"


def test_http_lists_unverified_facts_with_reason_and_retries_failed_run(monkeypatch):
    from backend.graph import workflow

    with TestClient(app) as client:
        workspace_id = _workspace(client, "facts-and-runs")
        hedged = _upload(
            client, workspace_id, "notes.md",
            "Alpha V2.0 上线日期可能是 2026-09-27。\n",
        )
        assert hedged.status_code == 201
        facts = client.get(f"/api/workspaces/{workspace_id}/facts").json()
        assert facts[0]["status"] == "unverified"
        assert facts[0]["artifact"] == "notes.md"
        assert "低于阈值" in facts[0]["review_reason"]

        original = workflow.start_run
        monkeypatch.setattr(workflow, "start_run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
        failed = _upload(client, workspace_id, "failed.md", "Alpha V2.0 上线日期 2026-09-20。\n")
        failed_thread = failed.json()["thread_id"]
        monkeypatch.setattr(workflow, "start_run", original)

        runs = client.get(f"/api/workspaces/{workspace_id}/runs").json()
        assert next(row for row in runs if row["thread_id"] == failed_thread)["status"] == "failed"
        retried = client.post(f"/api/runs/{failed_thread}/retry")
        assert retried.status_code == 200
        assert retried.json()["retried_from"] == failed_thread
        assert retried.json()["thread_id"] != failed_thread
