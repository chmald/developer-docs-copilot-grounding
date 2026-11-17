from dataclasses import replace
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from frontend import app as gateway
from scripts import foundry_iq

TENANT = "11111111-1111-4111-8111-111111111111"
SUBSCRIPTION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def settings():
    return gateway.Settings(
        project_url="https://example.services.ai.azure.com/api/projects/example",
        agent_name="document-qa", foundry_tenant=TENANT, foundry_subscription=SUBSCRIPTION,
        allowed_hosts=("testserver",),
    )


def completed(response_id="resp_first", annotations=None):
    return {
        "id": response_id, "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [{
            "type": "output_text", "text": "Use the documented sequence [1].",
            "annotations": annotations or [],
        }]}],
    }


def test_agent_chat_bypasses_legacy_and_keeps_followup_per_browser(settings, monkeypatch):
    calls = []

    def token(tenant, subscription, resource, timeout):
        assert (tenant, subscription, resource) == (TENANT, SUBSCRIPTION, "https://ai.azure.com/")
        return "server-only-token"

    async def handler(request):
        assert request.headers["authorization"] == "Bearer server-only-token"
        assert str(request.url).endswith("/api/projects/example/openai/v1/responses")
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=completed(f"resp_{len(calls)}", [{
            "type": "url_citation", "title": "Reference manual",
            "url": "https://example.blob.core.windows.net/raw/manual.pdf",
            "start_index": 0, "end_index": 20,
        }]))

    async def forbidden(*args, **kwargs):
        pytest.fail("Foundry chat must never call legacy retrieval or direct model generation")

    monkeypatch.setattr(foundry_iq, "cli_token", token)
    monkeypatch.setattr(gateway, "retrieve", forbidden)
    monkeypatch.setattr(gateway, "grounded_answer", forbidden)
    original = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    app = gateway.create_app(settings)
    with TestClient(app) as client, TestClient(app) as other:
        first = client.post("/api/chat", json={"query": "initial question"})
        assert first.status_code == 200, first.text
        assert first.json()["citations"][0]["title"] == "Reference manual"
        assert "server-only-token" not in first.text
        assert "httponly" in first.headers["set-cookie"].lower()
        assert other.post("/api/chat", json={
            "query": "steal context", "previous_response_id": "resp_1",
        }).status_code == 409
        followup = client.post("/api/chat", json={
            "query": "what happens next?", "previous_response_id": "resp_1",
        })
        assert followup.status_code == 200, followup.text
        assert calls[1]["previous_response_id"] == "resp_1"
        assert calls[1]["agent_reference"] == {"name": "document-qa", "type": "agent_reference"}
        assert "previous_response_id" not in calls[0]
        assert client.post("/api/chat", json={
            "query": "stale branch", "previous_response_id": "resp_1",
        }).status_code == 409
        assert client.post("/api/chat", json={"query": "start over"}).status_code == 200
        assert "previous_response_id" not in calls[-1]


@pytest.mark.parametrize("data", [
    {"status": "failed", "error": {"message": "secret upstream"}},
    {"status": "incomplete", "output": []},
    {"status": "completed", "output": [{"type": "mcp_approval_request"}]},
    {"status": "completed", "output": []},
])
def test_incomplete_or_unusable_agent_answers_fail(data):
    with pytest.raises(ValueError):
        foundry_iq.response_evidence(data)


def test_plain_agent_references_are_not_fabricated_as_urls():
    result = foundry_iq.response_evidence(completed())
    assert result["citations"] == []
    assert "not inferred" in result["notice"]
    assert result["answer"] == "Use the documented sequence [1]."


def test_agent_failure_is_redacted_and_never_falls_back(settings, monkeypatch):
    def fail(*args):
        raise ValueError("secret CLI output")

    monkeypatch.setattr(foundry_iq, "cli_token", fail)
    with TestClient(gateway.create_app(settings)) as client:
        response = client.post("/api/chat", json={"query": "question"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "agent_failed"
    assert "secret" not in response.text


def test_missing_foundry_configuration_stays_visible(settings):
    with TestClient(gateway.create_app(replace(settings, project_url=""))) as client:
        assert not client.get("/api/config").json()["chat_enabled"]
        assert client.post("/api/chat", json={"query": "question"}).status_code == 503


@pytest.mark.parametrize("url", [
    "https://example.com/api/projects/test",
    "https://example.services.ai.azure.com/openai/v1",
    "https://example.services.ai.azure.com/api/projects/test?token=secret",
    "http://example.services.ai.azure.com/api/projects/test",
])
def test_project_endpoint_rejects_non_foundry_targets(url):
    with pytest.raises(ValueError):
        foundry_iq.project_endpoint(url)
