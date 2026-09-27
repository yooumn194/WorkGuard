"""Expose only the Feishu webhook route to a temporary HTTPS tunnel.

This deliberately does not proxy the Change Center or any other WorkGuard API.
It is intended for local real-tenant acceptance tests, not as a production
reverse proxy.
"""
from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

WEBHOOK_PATH = "/api/integrations/feishu/webhook"
MAX_BODY_BYTES = 1024 * 1024
UPSTREAM = os.getenv(
    "WORKGUARD_WEBHOOK_UPSTREAM",
    "http://127.0.0.1:8765/api/integrations/feishu/webhook",
)


def forward_webhook(body: bytes, content_type: str, client: httpx.Client) -> httpx.Response:
    return client.post(
        UPSTREAM,
        content=body,
        headers={"Content-Type": content_type or "application/json"},
    )


class FeishuWebhookHandler(BaseHTTPRequestHandler):
    server_version = "WorkGuardFeishuGateway/1.0"

    def _reply(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path.split("?", 1)[0] != WEBHOOK_PATH:
            self._reply(404, b'{"detail":"not found"}')
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reply(400, b'{"detail":"invalid content length"}')
            return
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            self._reply(413, b'{"detail":"payload too large"}')
            return
        body = self.rfile.read(content_length)
        try:
            with httpx.Client(timeout=10.0) as client:
                response = forward_webhook(
                    body,
                    self.headers.get("Content-Type", "application/json"),
                    client,
                )
        except httpx.HTTPError:
            self._reply(502, b'{"detail":"upstream unavailable"}')
            return
        self._reply(response.status_code, response.content)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        self._reply(405, b'{"detail":"method not allowed"}')

    def log_message(self, format: str, *args: object) -> None:
        # Never log request bodies or verification tokens.
        super().log_message(format, *args)


def main() -> None:
    host = os.getenv("WORKGUARD_WEBHOOK_GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("WORKGUARD_WEBHOOK_GATEWAY_PORT", "8766"))
    server = ThreadingHTTPServer((host, port), FeishuWebhookHandler)
    print(f"Feishu-only gateway listening on http://{host}:{port}{WEBHOOK_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
