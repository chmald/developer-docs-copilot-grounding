import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from frontend import app as gateway

FAKE = Path(__file__).with_name("fake_mcp_server.py")
TENANT = "11111111-1111-4111-8111-111111111111"
SUBSCRIPTION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def settings():
    return gateway.Settings(qa_backend="legacy", command=(sys.executable, str(FAKE)), allowed_hosts=("testserver",), timeout=15)


@pytest.fixture
def client(settings):
    with TestClient(gateway.create_app(settings)) as client:
        yield client


def test_real_stdio_protocol_structured_provenance(client):
    response = client.post("/api/retrieve", json={"query": "pin routing", "top_k": 3})
    assert response.status_code == 200, response.text
    result = response.json()
    assert "top_k=3" in result["text"]
    citation = result["citations"][0]
    assert citation["title"] == "example-schematic.pdf"
    assert citation["metadata"]["pageNumberFrom"] == 3
    assert citation["metadata"]["extractionTier"] == "cu"
    assert result["raw"]["structuredContent"]["documents"][0]["contentKind"] == "diagram"


def test_real_stdio_text_compatibility(client):
    response = client.post("/api/retrieve", json={"query": "text"})
    assert response.status_code == 200, response.text
    assert response.json()["citations"][0]["title"] == "example.md — Initialization"


def test_default_and_maximum_top_k(client):
    response = client.post("/api/retrieve", json={"query": "pin routing"})
    assert response.status_code == 200
    assert "top_k=3" in response.json()["text"]
    assert client.post("/api/retrieve", json={"query": "pin routing", "top_k": 5}).status_code == 200
    config = client.get("/api/config").json()
    assert config["top_k_default"] == 3
    assert config["top_k_max"] == 5


def test_real_stdio_json_encoded_results(client):
    response = client.post("/api/retrieve", json={"query": "json-text"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["text"] == "[Developer guide]\nInitialize the interface before routing signals."
    assert data["citations"][0]["metadata"]["pageReferenceBasis"] == "split-part"
    assert data["citations"][0]["metadata"]["extractionTier"] == "di"
    assert json.loads(data["raw"]["content"][0]["text"])["mode"] == "knowledge-base"
    assert data["notice"] == "Showing 1 of 8 knowledge-base passages."


def test_real_streamable_http_protocol(settings):
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, str(FAKE), str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("Fixture HTTP MCP server exited before readiness")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    break
            except OSError:
                time.sleep(.05)
        else:
            pytest.fail("Fixture HTTP MCP server did not start")
        config = replace(settings, transport="streamable-http", url=f"http://127.0.0.1:{port}/mcp")
        with TestClient(gateway.create_app(config)) as client:
            response = client.post("/api/retrieve", json={"query": "http evidence"})
        assert response.status_code == 200, response.text
        assert "http evidence" in response.json()["text"]
        assert response.json()["citations"][0]["metadata"]["contentKind"] == "diagram"
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.parametrize("query,code", [("tool-error", "retrieval_failed"), ("slow", "retrieval_timeout")])
def test_real_protocol_errors(settings, query, code):
    timeout = 2 if query == "slow" else 15
    with TestClient(gateway.create_app(replace(settings, timeout=timeout))) as client:
        response = client.post("/api/retrieve", json={"query": query})
    assert response.status_code in {502, 504}, response.text
    assert response.json()["error"]["code"] == code


def test_real_tool_allowlist(settings):
    with TestClient(gateway.create_app(replace(settings, tool="not_allowed"))) as client:
        response = client.post("/api/retrieve", json={"query": "question"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "tool_unavailable"


@pytest.mark.parametrize("body", [
    {"query": ""}, {"query": "   "}, {"query": "x" * 4001},
    {"query": "x", "top_k": 0}, {"query": "x", "top_k": 6},
    {"query": "x", "top_k": 1.5}, {"query": "x", "top_k": True},
    {"query": "x", "url": "http://evil"}, {"query": "x", "command": ["whoami"]},
])
def test_input_constraints(client, body):
    response = client.post("/api/retrieve", json=body)
    assert response.status_code == 422
    assert "error" in response.json()


def test_origin_and_body_limits(client):
    assert client.post("/api/retrieve", json={"query": "x"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/retrieve", content="x" * 20_001).status_code == 413
    chunks = iter([b'{"query":"', b"x" * 20_001, b'"}'])
    assert client.post("/api/retrieve", content=chunks).status_code == 413


def test_local_ui_and_public_config_redaction(settings):
    secret = "never-in-browser"
    config = replace(settings, model_key=secret, token=secret, url=f"https://example.com/{secret}")
    with TestClient(gateway.create_app(config)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Content-Security-Policy" in response.headers
        assert client.get("/static/app.js").status_code == 200
        assert secret not in client.get("/api/config").text
        assert client.get("/missing").json()["error"]["code"] == "http_error"


@pytest.mark.parametrize("endpoint", ["", "http://example.com/openai/v1", "https://example.com/not-v1"])
def test_chat_configuration_errors(settings, endpoint):
    config = replace(settings, model_endpoint=endpoint, model_key="secret", model_deployment="configured-deployment")
    with TestClient(gateway.create_app(config)) as client:
        response = client.post("/api/chat", json={"query": "x"})
        assert not client.get("/api/config").json()["chat_enabled"]
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "chat_not_configured"


def test_optional_model_v1_request(settings, monkeypatch):
    captured = {}

    async def handler(request):
        captured["request"] = request
        return httpx.Response(200, json={"choices": [{"message": {"content": "Evidence-backed answer."}}]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    config = replace(settings, model_endpoint="https://example.com/openai/v1",
                     model_key="secret", model_deployment="my-deployed-model")
    answer = asyncio.run(gateway.grounded_answer(config, gateway.Question(query="x"), {"text": "source"}))
    assert answer == "Evidence-backed answer."
    request = captured["request"]
    assert str(request.url) == "https://example.com/openai/v1/chat/completions"
    assert request.headers["api-key"] == "secret"
    body = json.loads(request.content)
    assert body["model"] == "my-deployed-model"
    assert "source" in body["messages"][1]["content"]


def test_azure_cli_model_auth_checks_context_and_refreshes_token(settings, monkeypatch):
    commands, requests = [], []

    def run(args, **kwargs):
        commands.append(args)
        if args[1:3] == ["account", "show"]:
            output = json.dumps({"tenant": TENANT, "subscription": SUBSCRIPTION})
        else:
            assert args[1:3] == ["account", "get-access-token"]
            assert args[args.index("--tenant") + 1] == TENANT
            assert args[args.index("--resource") + 1] == "https://cognitiveservices.azure.com"
            output = f"renewed-token-{len(requests) + 1}"
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Grounded answer."}}]})

    monkeypatch.setattr(gateway.shutil, "which", lambda name: "az")
    monkeypatch.setattr(gateway.subprocess, "run", run)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    config = replace(
        settings, model_endpoint="https://example.openai.azure.com/openai/v1",
        model_deployment="deployment", model_auth="azure-cli",
        model_tenant_id=TENANT, model_subscription_id=SUBSCRIPTION,
    )
    for _ in range(2):
        assert asyncio.run(gateway.grounded_answer(config, gateway.Question(query="x"), {"text": "source"})) == "Grounded answer."
    assert [request.headers["authorization"] for request in requests] == [
        "Bearer renewed-token-1", "Bearer renewed-token-2",
    ]
    assert all("api-key" not in request.headers for request in requests)
    assert [args[2] for args in commands] == ["show", "get-access-token", "show", "get-access-token"]


@pytest.mark.parametrize("mismatch", ["tenant", "subscription"])
def test_azure_cli_rejects_context_drift_before_token(settings, monkeypatch, mismatch):
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        context = {"tenant": TENANT, "subscription": SUBSCRIPTION}
        context[mismatch] = "33333333-3333-4333-8333-333333333333"
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(context), stderr="")

    monkeypatch.setattr(gateway.shutil, "which", lambda name: "az")
    monkeypatch.setattr(gateway.subprocess, "run", run)
    config = replace(
        settings, model_endpoint="https://example.openai.azure.com/openai/v1",
        model_deployment="deployment", model_auth="azure-cli",
        model_tenant_id=TENANT, model_subscription_id=SUBSCRIPTION,
    )
    with pytest.raises(gateway.GatewayError) as raised:
        asyncio.run(gateway.grounded_answer(config, gateway.Question(query="x"), {"text": "source"}))
    assert raised.value.code == "model_context_mismatch"
    assert len(commands) == 1


@pytest.mark.parametrize("change", [
    {"model_tenant_id": ""},
    {"model_subscription_id": "not-a-guid"},
    {"model_endpoint": "https://untrusted.example/openai/v1"},
    {"model_auth": "unknown"},
])
def test_azure_cli_chat_configuration_fails_closed(settings, change):
    config = replace(
        settings, model_endpoint="https://example.openai.azure.com/openai/v1",
        model_deployment="deployment", model_auth="azure-cli",
        model_tenant_id=TENANT, model_subscription_id=SUBSCRIPTION,
    )
    with TestClient(gateway.create_app(replace(config, **change))) as client:
        assert not client.get("/api/config").json()["chat_enabled"]
        assert client.post("/api/chat", json={"query": "x"}).json()["error"]["code"] == "chat_not_configured"


def test_azure_cli_failure_does_not_expose_output(settings, monkeypatch):
    def run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="sensitive CLI output")

    monkeypatch.setattr(gateway.shutil, "which", lambda name: "az")
    monkeypatch.setattr(gateway.subprocess, "run", run)
    config = replace(
        settings, model_endpoint="https://example.openai.azure.com/openai/v1",
        model_deployment="deployment", model_auth="azure-cli",
        model_tenant_id=TENANT, model_subscription_id=SUBSCRIPTION,
    )
    with pytest.raises(gateway.GatewayError) as raised:
        asyncio.run(gateway.grounded_answer(config, gateway.Question(query="x"), {"text": "source"}))
    assert raised.value.code == "model_auth_failed"
    assert "sensitive" not in raised.value.message


def test_azure_cli_auth_timeout_is_reported(settings, monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, timeout=1)

    monkeypatch.setattr(gateway.shutil, "which", lambda name: "az")
    monkeypatch.setattr(gateway.subprocess, "run", run)
    config = replace(
        settings, model_endpoint="https://example.openai.azure.com/openai/v1",
        model_deployment="deployment", model_auth="azure-cli",
        model_tenant_id=TENANT, model_subscription_id=SUBSCRIPTION,
    )
    with pytest.raises(gateway.GatewayError) as raised:
        asyncio.run(gateway.grounded_answer(config, gateway.Question(query="x"), {"text": "source"}))
    assert raised.value.code == "model_auth_timeout"
    assert raised.value.status == 504


def test_environment_configures_azure_cli_auth(monkeypatch):
    monkeypatch.setenv("QA_BACKEND", "legacy")
    monkeypatch.setenv("AZURE_OPENAI_V1_ENDPOINT", "https://example.openai.azure.com/openai/v1/")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "deployment")
    monkeypatch.setenv("AZURE_OPENAI_AUTH_MODE", "azure-cli")
    monkeypatch.setenv("AZURE_OPENAI_TENANT_ID", TENANT)
    monkeypatch.setenv("AZURE_OPENAI_SUBSCRIPTION_ID", SUBSCRIPTION)
    config = gateway.Settings.from_env()
    assert config.chat_error is None
    assert (config.model_auth, config.model_tenant_id, config.model_subscription_id) == (
        "azure-cli", TENANT, SUBSCRIPTION,
    )


@pytest.mark.parametrize("failure", ["http", "timeout", "malformed"])
def test_optional_model_errors(settings, monkeypatch, failure):
    async def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret upstream configuration", request=request)
        if failure == "http":
            return httpx.Response(401, text="secret upstream configuration")
        return httpx.Response(200, json={"unexpected": "secret upstream configuration"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    config = replace(settings, model_endpoint="https://example.com/openai/v1",
                     model_key="secret", model_deployment="configured-deployment")
    with pytest.raises(gateway.GatewayError) as raised:
        asyncio.run(gateway.grounded_answer(config, gateway.Question(query="x"), {"text": "source"}))
    assert raised.value.code == ("model_timeout" if failure == "timeout" else "model_failed")
    assert "secret" not in raised.value.message


def test_model_failure_preserves_evidence(client, monkeypatch):
    async def fail(*args):
        raise gateway.GatewayError("model_failed", "Model unavailable.")

    monkeypatch.setattr(gateway, "grounded_answer", fail)
    config = gateway.Settings(qa_backend="legacy", command=(sys.executable, str(FAKE)), allowed_hosts=("testserver",),
                              model_endpoint="https://example.com/openai/v1",
                              model_key="secret", model_deployment="deployment")
    with TestClient(gateway.create_app(config)) as configured:
        response = configured.post("/api/chat", json={"query": "text"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "model_failed"
    assert "Evidence only." in response.json()["retrieval"]["text"]


@pytest.mark.parametrize("name,value", [
    ("FRONTEND_MCP_COMMAND", '"not-an-array"'),
    ("FRONTEND_MCP_TRANSPORT", "sse"),
    ("FRONTEND_TIMEOUT_SECONDS", "0"),
    ("MCP_TOOL_NAME", "../unsafe"),
])
def test_invalid_environment_fails_closed(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        gateway.Settings.from_env()


def test_remote_http_requires_tls(monkeypatch):
    monkeypatch.setenv("FRONTEND_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("FRONTEND_MCP_URL", "http://remote.example/mcp")
    with pytest.raises(ValueError):
        gateway.Settings.from_env()
