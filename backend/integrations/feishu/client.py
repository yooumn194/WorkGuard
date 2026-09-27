"""Feishu (Lark) Open API adapter.

A deliberately thin HTTP client over the documented Open API endpoints — no
SDK dependency. Every method returns plain dicts/bytes so the service layer
stays trivially mockable in tests. Nothing here touches WorkGuard's domain
model; that mapping lives in service.py.

Endpoints used (app-tenant identity):
- POST /open-apis/auth/v3/tenant_access_token/internal   (token, cached)
- GET  /open-apis/drive/v1/files?folder_token=...        (list folder)
- POST /open-apis/drive/v1/files/{file_token}/subscribe  (subscribe file events)
- POST /open-apis/drive/v1/export_tasks                  (create docx export)
- GET  /open-apis/drive/v1/export_tasks/{ticket}         (poll export)
- GET  /open-apis/drive/v1/export_tasks/file/{file_token}/download (download bytes)
- GET  /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records
- POST {custom bot webhook url}                          (approval notifications)
"""
from __future__ import annotations

import json
import threading
import time

import httpx

from backend.config import settings

_TIMEOUT = httpx.Timeout(15.0)


class FeishuError(RuntimeError):
    pass


class FeishuClient:
    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        base_url: str | None = None,
        webhook_url: str | None = None,
        notify_receive_id: str | None = None,
        notify_receive_id_type: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        # None means "use configured default"; an explicit empty string means
        # "disabled". Keeping those states distinct makes tests and callers
        # fail closed instead of accidentally using production credentials.
        self._app_id = settings.feishu_app_id if app_id is None else app_id
        self._app_secret = settings.feishu_app_secret if app_secret is None else app_secret
        self._base_url = (
            settings.feishu_base_url if base_url is None else base_url
        ).rstrip("/")
        self._webhook_url = (
            settings.feishu_webhook_url if webhook_url is None else webhook_url
        )
        self._notify_receive_id = (
            settings.feishu_notify_receive_id
            if notify_receive_id is None else notify_receive_id
        )
        self._notify_receive_id_type = (
            settings.feishu_notify_receive_id_type
            if notify_receive_id_type is None else notify_receive_id_type
        )
        self._http = http_client or httpx.Client(timeout=_TIMEOUT)
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._app_id and self._app_secret)

    @property
    def webhook_configured(self) -> bool:
        return bool(self._webhook_url)

    @property
    def app_notification_configured(self) -> bool:
        return bool(self.configured and self._notify_receive_id)

    # ------------------------------------------------------------- internal
    def _post(self, path: str, json_body: dict | None = None, token: str | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = self._http.post(
            f"{self._base_url}{path}", json=json_body or {}, headers=headers
        )
        if response.status_code >= 400:
            raise FeishuError(f"{path} -> HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def _get(self, path: str, token: str, params: dict | None = None) -> dict:
        response = self._http.get(
            f"{self._base_url}{path}", params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code >= 400:
            raise FeishuError(f"{path} -> HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def tenant_access_token(self) -> str:
        """Fetch (and cache until expiry) the app tenant access token."""
        with self._token_lock:
            if self._token and time.time() < self._token_expires_at - 60:
                return self._token
            if not self.configured:
                raise FeishuError("Feishu is not configured (FEISHU_APP_ID / FEISHU_APP_SECRET)")
            data = self._post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": self._app_id, "app_secret": self._app_secret},
            )
            if data.get("code") != 0:
                raise FeishuError(f"tenant_access_token failed: {data}")
            self._token = data["tenant_access_token"]
            self._token_expires_at = time.time() + int(data.get("expire", 7200))
            return self._token

    # ------------------------------------------------------------- drive
    def list_folder_files(self, folder_token: str) -> list[dict]:
        """List files in a drive folder: [{token, name, type, url}].

        ``root`` is a WorkGuard sentinel for the application's own Drive root.
        Feishu represents that location by omitting ``folder_token`` entirely;
        keeping the sentinel explicit prevents an accidentally blank binding.
        """
        token = self.tenant_access_token()
        files: list[dict] = []
        page_token = ""
        while True:
            params: dict[str, int | str] = {"page_size": 50}
            if folder_token != "root":
                params["folder_token"] = folder_token
            if page_token:
                params["page_token"] = page_token
            data = self._get("/open-apis/drive/v1/files", token, params)
            if data.get("code") != 0:
                raise FeishuError(f"list_folder_files failed: {data}")
            payload = data.get("data", {})
            for item in payload.get("files", []):
                files.append({
                    "token": item.get("token"),
                    "name": item.get("name"),
                    "type": item.get("type"),  # doc | docx | sheet | bitable | file
                    "url": item.get("url"),
                })
            if not payload.get("has_more"):
                break
            next_page = payload.get("page_token", "")
            if not next_page or next_page == page_token:
                raise FeishuError("list_folder_files returned has_more without a new page_token")
            page_token = next_page
        return files

    def export_doc_as_docx(self, file_token: str, timeout_seconds: int = 20) -> bytes:
        """Export a Feishu doc/sheet to DOCX bytes via the export-task pipeline."""
        token = self.tenant_access_token()
        created = self._post(
            "/open-apis/drive/v1/export_tasks",
            {"file_extension": "docx", "token": file_token, "type": "docx"},
            token=token,
        )
        if created.get("code") != 0:
            raise FeishuError(f"export_task create failed: {created}")
        ticket = created["data"]["ticket"]
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            status = self._get(f"/open-apis/drive/v1/export_tasks/{ticket}", token,
                               params={"token": file_token})
            if status.get("code") != 0:
                raise FeishuError(f"export_task poll failed: {status}")
            result = status["data"]["result"]
            if result.get("job_status") == 0:  # success
                # The real API returns an ephemeral ``file_token`` here, not
                # a URL.  It must be exchanged at the documented download
                # endpoint with the same tenant credential.
                export_file_token = result.get("file_token")
                if not export_file_token:
                    raise FeishuError("export task succeeded without a file_token")
                downloaded = self._http.get(
                    f"{self._base_url}/open-apis/drive/v1/export_tasks/file/"
                    f"{export_file_token}/download",
                    headers={"Authorization": f"Bearer {token}"},
                )
                downloaded.raise_for_status()
                return downloaded.content
            if result.get("job_status") in (1, 2):  # pending / processing
                time.sleep(0.5)
                continue
            raise FeishuError(f"export task failed: {result}")
        raise FeishuError(f"export task timed out after {timeout_seconds}s")

    def subscribe_file_events(self, file_token: str, file_type: str) -> dict:
        """Subscribe the app identity to events for one managed cloud document.

        Registering ``drive.file.edit_v1`` in the developer console is only
        the first half of Feishu's contract. Each document must also be
        subscribed through this endpoint before edits generate webhooks.
        """
        if file_type not in {"doc", "docx", "sheet", "bitable", "file"}:
            raise FeishuError(f"unsupported event subscription type: {file_type}")
        token = self.tenant_access_token()
        response = self._http.post(
            f"{self._base_url}/open-apis/drive/v1/files/{file_token}/subscribe",
            params={"file_type": file_type},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        if response.status_code >= 400:
            raise FeishuError(
                f"subscribe_file_events -> HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        data = response.json()
        if data.get("code") != 0:
            raise FeishuError(f"subscribe_file_events failed: {data}")
        return data

    # ------------------------------------------------------------- bitable
    def list_bitable_records(self, app_token: str, table_id: str) -> list[dict]:
        """List all records of a Bitable table: [{record_id, fields}]."""
        token = self.tenant_access_token()
        records: list[dict] = []
        page_token = ""
        while True:
            params: dict[str, int | str] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._get(
                f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                token, params,
            )
            if data.get("code") != 0:
                raise FeishuError(f"list_bitable_records failed: {data}")
            payload = data.get("data", {})
            for item in payload.get("items", []):
                records.append({"record_id": item.get("record_id"), "fields": item.get("fields", {})})
            if not payload.get("has_more"):
                break
            next_page = payload.get("page_token", "")
            if not next_page or next_page == page_token:
                raise FeishuError("list_bitable_records returned has_more without a new page_token")
            page_token = next_page
        return records

    def list_bitable_tables(self, app_token: str) -> list[dict]:
        token = self.tenant_access_token()
        data = self._get(
            f"/open-apis/bitable/v1/apps/{app_token}/tables", token,
            params={"page_size": 100},
        )
        if data.get("code") != 0:
            raise FeishuError(f"list_bitable_tables failed: {data}")
        return list(data.get("data", {}).get("items", []))

    @staticmethod
    def bitable_records_to_markdown(title: str, records: list[dict]) -> str:
        """Flatten Bitable records into a markdown key/value document that the
        existing parsers + extractors already understand."""
        lines = [f"# {title}", ""]
        for record in records:
            fields = record.get("fields", {})
            for key, value in fields.items():
                if isinstance(value, dict):  # rich text / person / etc.
                    value = value.get("text") or value.get("name") or str(value)
                elif isinstance(value, list):
                    value = ", ".join(
                        str(v.get("text") or v.get("name") or v) if isinstance(v, dict) else str(v)
                        for v in value
                    )
                if value in (None, ""):
                    continue
                lines.append(f"{key}: {value}")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------- webhook
    def send_webhook_text(self, text: str) -> dict:
        """Send a message through a custom-bot webhook (group notification)."""
        if not self.webhook_configured:
            raise FeishuError("Feishu webhook is not configured (FEISHU_WEBHOOK_URL)")
        response = self._http.post(
            self._webhook_url,
            json={"msg_type": "text", "content": {"text": text}},
        )
        if response.status_code >= 400:
            raise FeishuError(f"webhook -> HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        if data.get("code", data.get("StatusCode", 0)) not in (0, None):
            raise FeishuError(f"webhook rejected message: {data}")
        return data

    def send_app_text(self, text: str) -> dict:
        """Send a text notification using the published app bot.

        This is the preferred production path: unlike a custom webhook it is
        tied to the application's tenant identity and ``im:message`` scope.
        The destination is explicit so WorkGuard never guesses a recipient.
        """
        if not self.app_notification_configured:
            raise FeishuError(
                "Feishu app notification is not configured "
                "(FEISHU_NOTIFY_RECEIVE_ID)"
            )
        allowed = {"chat_id", "open_id", "user_id", "union_id", "email"}
        if self._notify_receive_id_type not in allowed:
            raise FeishuError(
                f"unsupported FEISHU_NOTIFY_RECEIVE_ID_TYPE: {self._notify_receive_id_type}"
            )
        token = self.tenant_access_token()
        response = self._http.post(
            f"{self._base_url}/open-apis/im/v1/messages",
            params={"receive_id_type": self._notify_receive_id_type},
            json={
                "receive_id": self._notify_receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            raise FeishuError(
                f"app bot -> HTTP {response.status_code}: {response.text[:200]}"
            )
        data = response.json()
        if data.get("code") != 0:
            raise FeishuError(f"app bot rejected message: {data}")
        return data
