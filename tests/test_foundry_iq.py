import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from scripts import foundry_iq
from scripts.configure_foundry_iq import wait_for_indexers
from scripts import configure_foundry_iq as setup

TENANT = "11111111-1111-4111-8111-111111111111"
SUBSCRIPTION = "22222222-2222-4222-8222-222222222222"


def test_cli_context_mismatch_prevents_token_acquisition(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({
            "tenant": TENANT, "subscription": "33333333-3333-4333-8333-333333333333",
        }))

    monkeypatch.setattr(foundry_iq.shutil, "which", lambda _: "az")
    monkeypatch.setattr(foundry_iq.subprocess, "run", run)
    with pytest.raises(ValueError, match="does not match"):
        foundry_iq.cli_token(TENANT, SUBSCRIPTION, "https://ai.azure.com/")
    assert len(calls) == 1
    assert "get-access-token" not in calls[0]


def test_readiness_waits_until_every_indexer_succeeds():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"lastResult": {
            "status": "inProgress" if len(calls) == 1 else "success", "itemsFailed": 0,
        }})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        wait_for_indexers(client, "https://example.search.windows.net", {}, ["cu", "di"], poll=0)
    assert len(calls) == 4


@pytest.mark.parametrize("last", [
    {"status": "success", "itemsFailed": 1},
    {"status": "persistentFailure", "itemsFailed": 0},
])
def test_readiness_rejects_failed_documents(last):
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"lastResult": last}),
    )) as client:
        with pytest.raises(ValueError, match="readiness"):
            wait_for_indexers(client, "https://example.search.windows.net", {}, ["cu"])


def test_readiness_cannot_pass_without_a_run_or_indexer():
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"lastResult": None}),
    )) as client:
        with pytest.raises(ValueError, match="No indexers"):
            wait_for_indexers(client, "https://example.search.windows.net", {}, [])
        with pytest.raises(TimeoutError):
            wait_for_indexers(client, "https://example.search.windows.net", {}, ["cu"], timeout=0)


@pytest.fixture
def deployment(monkeypatch, tmp_path):
    requests, definitions, roles, saved = [], [], [], []
    state = {"count": 10, "failed": 0}
    ids = {
        "tenantId": TENANT, "subscriptionId": SUBSCRIPTION, "resourceGroup": "example-rg",
        "foundryResource": "example-foundry", "foundryProject": "example-project",
        "searchEndpoint": "https://example.search.windows.net", "searchService": "example",
        "chatDeployment": "existing-answer-model",
    }

    def handler(request):
        requests.append(request)
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(200, json={"status": "completed", "output": [{
                "type": "message", "role": "assistant", "content": [{
                    "type": "output_text", "text": "The corpus contains example manuals.",
                }],
            }]})
        if request.method == "PUT":
            return httpx.Response(200, json={})
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        if "/knowledgebases/" in path:
            return httpx.Response(200, json={
                "@odata.etag": "old-etag", "name": "kb-hybrid", "outputMode": "answerSynthesis",
                "knowledgeSources": [{"name": "existing-source"}],
                "models": [{"kind": "azureOpenAI", "azureOpenAIParameters": {"deploymentId": "planner"}}],
            })
        if "/knowledgesources/" in path:
            return httpx.Response(200, json={
                "kind": "searchIndex", "searchIndexParameters": {"searchIndexName": "existing-index"},
            })
        if path.endswith("/indexers"):
            return httpx.Response(200, json={"value": [
                {"name": "cu", "targetIndexName": "existing-index"},
                {"name": "unrelated", "targetIndexName": "another-index"},
            ]})
        if path.endswith("/status"):
            return httpx.Response(200, json={"lastResult": {"status": "success", "itemsFailed": state["failed"]}})
        if path.endswith("/docs/$count"):
            return httpx.Response(200, text=str(state["count"]))
        if "/projects/" in path:
            return httpx.Response(200, json={"identity": {"principalId": "project-principal"}})
        if "/searchServices/" in path:
            return httpx.Response(200, json={"properties": {"authOptions": {"apiKeyOnly": {}}, "disableLocalAuth": False}})
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    client_type = httpx.Client
    monkeypatch.setattr(setup.httpx, "Client", lambda **_: client_type(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(setup.foundry_iq, "cli_context", lambda *_: "az")
    monkeypatch.setattr(setup.foundry_iq, "cli_token", lambda *_: "test-token")
    monkeypatch.setattr(setup.pds, "get_admin_key", lambda _: "test-key")
    monkeypatch.setattr(setup.pds, "save_ids", lambda path, value: saved.append(json.loads(json.dumps(value))))
    monkeypatch.setattr(setup.subprocess, "run", lambda args, **_: roles.append(args))
    import azure.ai.projects
    import azure.identity

    def create_version(**kwargs):
        definitions.append(kwargs)
        return SimpleNamespace(name=kwargs["agent_name"], version="1")

    project = MagicMock()
    project.__enter__.return_value = project
    project.agents.create_version.side_effect = create_version
    monkeypatch.setattr(azure.ai.projects, "AIProjectClient", lambda **_: project)
    monkeypatch.setattr(azure.identity, "AzureCliCredential", lambda **_: MagicMock())
    return SimpleNamespace(
        ids=ids, path=tmp_path / "demo-ids.local.json", requests=requests,
        definitions=definitions, roles=roles, saved=saved, state=state,
        args=SimpleNamespace(
            knowledge_base=None, indexer=None, timeout=1, check=False,
            force=False, smoke_query="What documents are available?",
        ),
    )


def test_setup_preserves_kb_and_uses_project_identity_allowlisted_tool(deployment):
    d = deployment
    setup.configure(d.ids, d.path, d.args)
    writes = [request for request in d.requests if request.method == "PUT"]
    assert len(writes) == 2
    kb = json.loads(writes[0].content)
    assert kb["outputMode"] == "extractiveData"
    assert kb["models"][0]["azureOpenAIParameters"]["deploymentId"] == "planner"
    assert kb["knowledgeSources"] == [{"name": "existing-source"}]
    assert "@odata.etag" not in kb
    connection = json.loads(writes[1].content)["properties"]
    assert connection["authType"] == "ProjectManagedIdentity"
    assert connection["category"] == "RemoteTool"
    assert connection["audience"] == "https://search.azure.com/"
    assert connection["target"].endswith("/knowledgebases/kb-hybrid/mcp?api-version=2026-08-01-preview")
    definition = d.definitions[0]["definition"].as_dict()
    assert definition["model"] == "existing-answer-model"
    tool, = definition["tools"]
    assert tool["allowed_tools"] == ["knowledge_base_retrieve"]
    assert tool["require_approval"] == "never"
    assert tool["project_connection_id"] == "document-knowledge"
    assert "--subscription" in d.roles[0]
    assert "project-principal" in d.roles[0]
    assert d.saved[0]["foundryAgent"]["version"] == "1"
    assert d.requests[-1].url.path.endswith("/openai/v1/responses")
    auth_update, = [request for request in d.requests if request.method == "PATCH"]
    assert json.loads(auth_update.content) == {"properties": {"authOptions": {"aadOrApiKey": {
        "aadAuthFailureMode": "http401WithBearerChallenge",
    }}}}


@pytest.mark.parametrize("failure", ["empty", "failed", "unrelated"])
def test_setup_blocks_resource_writes_when_readiness_fails(deployment, failure):
    d = deployment
    if failure == "empty":
        d.state["count"] = 0
    elif failure == "failed":
        d.state["failed"] = 1
    else:
        d.args.indexer = ["unrelated"]
    with pytest.raises(ValueError):
        setup.configure(d.ids, d.path, d.args)
    assert all(request.method == "GET" for request in d.requests)
    assert not d.roles and not d.definitions and not d.saved


def test_check_mode_does_not_configure_resources(deployment):
    d = deployment
    d.args.check = True
    setup.configure(d.ids, d.path, d.args)
    assert all(request.method in {"GET", "POST"} for request in d.requests)
    assert not d.roles and not d.definitions and not d.saved
    assert d.requests[-1].method == "POST"


def test_saved_fingerprint_skips_duplicate_agent_version(deployment):
    d = deployment
    setup.configure(d.ids, d.path, d.args)
    setup.configure(d.ids, d.path, d.args)
    assert len(d.definitions) == 1
