import httpx

from scripts.feishu_webhook_gateway import WEBHOOK_PATH, forward_webhook


def test_gateway_forwards_only_payload_and_content_type():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"challenge": "ok"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = forward_webhook(b'{"challenge":"ok"}', "application/json", client)

    assert response.status_code == 200
    assert requests[0].url.path == WEBHOOK_PATH
    assert requests[0].content == b'{"challenge":"ok"}'
    assert requests[0].headers["content-type"] == "application/json"
