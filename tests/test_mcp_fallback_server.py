"""Smoke tests for scripts/mcp_fallback_server.py -- confirm the module
imports cleanly (with required env vars stubbed) and its retrieval helpers
return a correctly-shaped result against a mocked AI Search response.

NOTE: these tests target the module's internal retrieval helpers directly
rather than invoking the @mcp.tool()-decorated function through the MCP
protocol layer -- that round-trip is exercised by the functional test in
04-testing.md § A instead."""
import os
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# mcp_fallback_server.py reads its Search connection settings from environment
# variables at import time -- stub them before importing so this test doesn't
# need a live Search service.
os.environ.setdefault("SEARCH_ENDPOINT", "https://srch-ddmcp-dev-eastus2.search.windows.net")
os.environ.setdefault("SEARCH_API_KEY", "fake-key")
os.environ.setdefault("SEARCH_INDEX_NAME", "idx-documents")
os.environ.setdefault("KNOWLEDGE_BASE_NAME", "kb-documents")

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import mcp_fallback_server as server  # noqa: E402


def test_module_imports():
    assert server is not None
    assert server.SEARCH_INDEX_NAME == "idx-documents"


def test_tool_is_defined():
    assert server.retrieve_documents is not None


def test_tool_identity_is_configurable():
    """GitHub Copilot decides whether to call an MCP tool from its name and
    description, so both must be settable per corpus without a code edit."""
    assert server.MCP_TOOL_NAME
    assert server.MCP_TOOL_DESCRIPTION
    # Defaults must stay corpus-neutral; a real corpus overrides them via env.
    for domain_term in ("datasheet", "firmware", "hardware"):
        assert domain_term not in server.MCP_TOOL_DESCRIPTION.lower()


def test_tool_registered_under_configured_name():
    """The registered tool name should follow MCP_TOOL_NAME, not the Python
    function name -- that's what makes the server reusable across corpora."""
    import asyncio

    tools = asyncio.run(server.mcp.list_tools())
    assert [t.name for t in tools] == [server.MCP_TOOL_NAME]


def test_semantic_config_matches_index_builder_constant():
    """mcp_fallback_server queries with a semantic configuration name that
    post_deploy_search creates -- a mismatch silently breaks direct search."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    import post_deploy_search as pds

    assert server.SEMANTIC_CONFIG_NAME == pds.SEMANTIC_CONFIG_NAME


@pytest.mark.asyncio
async def test_retrieve_via_knowledge_base_propagates_http_error():
    with patch("mcp_fallback_server.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.side_effect = server.httpx.HTTPError("boom")
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        with pytest.raises(server.httpx.HTTPError, match="boom"):
            await server._retrieve_via_knowledge_base("test question")


@pytest.mark.asyncio
async def test_retrieve_via_direct_search_formats_citations():
    fake_response = MagicMock()
    fake_response.raise_for_status = lambda: None
    fake_response.json.return_value = {
        "value": [
            {
                "content": "Timer1 resets to 0x00.",
                "sourceDocument": "manual.pdf",
                "sectionH1": "Peripherals",
                "sectionH2": "Timer Registers",
            }
        ]
    }

    with patch("mcp_fallback_server.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = fake_response
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await server._retrieve_via_direct_search("what is the reset value of Timer1?", top_k=5)

    assert "manual.pdf" in result
    # Heading path, not page number -- the Layout skill's markdown mode emits
    # section headings; page numbers require text mode + locationMetadata.
    assert "Peripherals > Timer Registers" in result
    assert "Timer1 resets to 0x00." in result


def test_heading_formatter_handles_partial_and_empty_headings():
    assert server._format_heading({"sectionH1": "A", "sectionH2": "B", "sectionH3": "C"}) == "A > B > C"
    assert server._format_heading({"sectionH1": "A", "sectionH3": "C"}) == "A > C"
    assert server._format_heading({}) == "untitled section"


@pytest.mark.asyncio
async def test_retrieve_via_direct_search_handles_empty_results():
    fake_response = MagicMock()
    fake_response.raise_for_status = lambda: None
    fake_response.json.return_value = {"value": []}

    with patch("mcp_fallback_server.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = fake_response
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await server._retrieve_via_direct_search("a question the corpus can't answer", top_k=5)

    assert "No matching content" in result


@pytest.mark.asyncio
async def test_hybrid_direct_search_preserves_source_fields(monkeypatch):
    monkeypatch.setattr(server, "SEARCH_INDEX_SCHEMA", "hybrid")
    response = MagicMock()
    response.json.return_value = {"value": [{
        "chunkId": "figure-1", "content": '{"summary":"U1 connects to J2"}',
        "sourceDocument": "manual__p0301-0600.pdf",
        "sourceUri": "https://example.test/manual__p0301-0600.pdf",
        "sectionLabel": "Board schematic", "pageNumberFrom": 3, "pageNumberTo": 4,
        "extractionTier": "content-understanding", "contentKind": "image-description",
    }]}
    with patch.object(server.httpx, "AsyncClient") as factory:
        client = AsyncMock()
        client.post.return_value = response
        factory.return_value.__aenter__.return_value = client
        payload = json.loads(await server._retrieve_via_direct_search("U1", 3))
    result = payload["results"][0]
    assert result["sectionLabel"] == "Board schematic"
    assert result["pageNumberFrom"] == result["pageStart"] == 3
    assert result["originalDocument"] == "manual.pdf"
    assert result["originalPageNumberFrom"] == 303
    assert result["originalPageNumberTo"] == 304
    assert result["pageReferenceBasis"] == "split-part"
    assert result["contentKind"] == "image-description"
    assert result["extractionTier"] == "content-understanding"
    assert result["url"] == "https://example.test/manual__p0301-0600.pdf"
    selected = client.post.call_args.kwargs["json"]["select"]
    assert "pageNumberFrom" in selected and "sectionH1" not in selected


def test_missing_pages_and_uris_are_not_invented():
    result = server._source_result({"sourceDocument": "manual__p0301-0600.pdf"})
    assert result["url"] is None
    assert result["pageNumberFrom"] is None
    assert "originalPageNumberFrom" not in result


@pytest.mark.asyncio
async def test_knowledge_base_returns_all_response_text_and_citations():
    response = MagicMock()
    response.json.return_value = {
        "response": [{"content": [{"text": "first"}, {"text": "second"}]}],
        "references": [{"id": "ref-7", "sourceData": {
            "content": "U1 connects to J2", "sourceDocument": "board.pdf",
            "pageNumberFrom": 7, "pageNumberTo": 7,
            "contentKind": "image-description", "sectionLabel": "Circuit",
        }}],
    }
    with patch.object(server.httpx, "AsyncClient") as factory:
        client = AsyncMock()
        client.post.return_value = response
        factory.return_value.__aenter__.return_value = client
        result = json.loads(await server._retrieve_via_knowledge_base("connections"))
    assert result["answer"] == "first\nsecond"
    assert result["results"][0]["id"] == "ref-7"
    assert result["results"][0]["text"] == "U1 connects to J2"
    assert result["results"][0]["pageNumberFrom"] == 7


@pytest.mark.asyncio
async def test_knowledge_base_limits_passages_before_encoding():
    response = MagicMock()
    response.json.return_value = {
        "response": [{"content": [{"text": "excluded response " * 20_000}]}],
        "references": [
            {"id": str(i), "sourceData": {
                "content": f"passage {i}" if i < 2 else "excluded source " * 20_000,
                "sourceDocument": f"manual-{i}.pdf", "pageNumberFrom": i + 1,
            }}
            for i in range(3)
        ],
    }
    with patch.object(server.httpx, "AsyncClient") as factory:
        client = AsyncMock()
        client.post.return_value = response
        factory.return_value.__aenter__.return_value = client
        encoded = await server._retrieve_via_knowledge_base("question", 2)
    payload = json.loads(encoded)
    assert [item["id"] for item in payload["results"]] == ["0", "1"]
    assert payload["results"][1]["pageNumberFrom"] == 2
    assert payload["answer"] == ""
    assert payload["limit"] == {"requested": 2, "returned": 2, "available": 3}
    assert "Showing 2 of 3" in payload["message"]
    assert "excluded" not in encoded.replace(payload["message"], "")


@pytest.mark.asyncio
async def test_requested_top_k_reaches_knowledge_base(monkeypatch):
    kb = AsyncMock(return_value="bounded")
    monkeypatch.setattr(server, "_retrieve_via_knowledge_base", kb)
    assert await server.retrieve_documents("question", 2) == "bounded"
    kb.assert_awaited_once_with("question", 2)


@pytest.mark.asyncio
async def test_direct_fallback_requires_explicit_disabled_knowledge_base(monkeypatch):
    direct = AsyncMock(return_value="direct")
    monkeypatch.setattr(server, "_retrieve_via_direct_search", direct)
    kb = AsyncMock(side_effect=server.httpx.HTTPError("service unavailable"))
    monkeypatch.setattr(server, "_retrieve_via_knowledge_base", kb)
    with pytest.raises(server.httpx.HTTPError):
        await server.retrieve_documents("question")
    direct.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_knowledge_base_uses_direct_search(monkeypatch):
    monkeypatch.setattr(server, "KNOWLEDGE_BASE_NAME", "")
    direct = AsyncMock(return_value="direct")
    monkeypatch.setattr(server, "_retrieve_via_direct_search", direct)
    assert await server.retrieve_documents("question", 2) == "direct"
    direct.assert_awaited_once_with("question", 2)


@pytest.mark.asyncio
async def test_missing_config_fails_before_network(monkeypatch):
    monkeypatch.setattr(server, "SEARCH_ENDPOINT", "")
    with pytest.raises(ValueError, match="SEARCH_ENDPOINT"):
        await server.retrieve_documents("question")


@pytest.mark.asyncio
@pytest.mark.parametrize("query,top_k", [("", 5), ("q", 0), ("q", 51), ("q", True)])
async def test_invalid_tool_arguments_fail_before_network(query, top_k):
    with pytest.raises(ValueError):
        await server.retrieve_documents(query, top_k)


@pytest.mark.asyncio
async def test_streamable_http_initialization_and_tool_call(monkeypatch):
    """Exercise the real SDK's HTTP transport and JSON-RPC session lifecycle."""
    monkeypatch.setattr(server, "_retrieve_via_knowledge_base",
                        AsyncMock(return_value='{"results":[{"text":"grounded evidence"}]}'))
    app = server.mcp.streamable_http_app()
    headers = {"Accept": "application/json, text/event-stream"}

    def rpc_payload(response):
        if "text/event-stream" in response.headers.get("content-type", ""):
            return json.loads(next(line[5:].strip() for line in response.text.splitlines()
                                   if line.startswith("data:")))
        return response.json()

    async with server.mcp.session_manager.run():
        async with server.httpx.AsyncClient(
            transport=server.httpx.ASGITransport(app=app), base_url="http://localhost",
            headers=headers,
        ) as client:
            response = await client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "regression-test", "version": "1"}},
            })
            assert response.status_code == 200
            assert "serverInfo" in rpc_payload(response)["result"]
            client.headers["mcp-session-id"] = response.headers["mcp-session-id"]
            client.headers["mcp-protocol-version"] = "2025-06-18"
            response = await client.post("/mcp", json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })
            assert response.status_code == 202
            response = await client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            })
            tool = rpc_payload(response)["result"]["tools"][0]
            assert tool["name"] == server.MCP_TOOL_NAME
            assert {"query", "top_k"} <= set(tool["inputSchema"]["properties"])
            response = await client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": server.MCP_TOOL_NAME, "arguments": {"query": "q", "top_k": 2}},
            })
            result = rpc_payload(response)["result"]
            assert not result.get("isError")
            assert json.loads(result["content"][0]["text"])["results"][0]["text"] == "grounded evidence"


def test_container_binding_requires_explicit_allowed_hosts(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["server", "--transport", "streamable-http", "--host", "0.0.0.0"])
    monkeypatch.setattr(server, "MCP_ALLOWED_HOSTS", [])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_real_stdio_sdk_retrieval_round_trip():
    """Run the shipped executable with the pinned client against local Search."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    class SearchFixture(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["search"] == "diagram labels"
            payload = json.dumps({"value": [{
                "chunkId": "fixture-1", "content": "Visible label U1 connects to J2.",
                "sourceDocument": "board.pdf", "pageNumberFrom": 9, "pageNumberTo": 9,
                "extractionTier": "di-layout-verbalized", "contentKind": "image-description",
            }]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), SearchFixture)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(server.__file__).resolve()), "--transport", "stdio"],
            env={**os.environ, "SEARCH_ENDPOINT": f"http://127.0.0.1:{fixture.server_port}",
                 "SEARCH_API_KEY": "fixture-key", "KNOWLEDGE_BASE_NAME": "",
                 "SEARCH_INDEX_SCHEMA": "hybrid", "MCP_TOOL_NAME": "retrieve_documents"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert tools.tools[0].name == "retrieve_documents"
                result = await session.call_tool(
                    "retrieve_documents", {"query": "diagram labels", "top_k": 1},
                )
                assert not result.isError
                citation = json.loads(result.content[0].text)["results"][0]
                assert citation["title"] == "board.pdf"
                assert citation["pageNumberFrom"] == 9
                assert citation["contentKind"] == "image-description"
    finally:
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=5)
