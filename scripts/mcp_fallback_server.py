"""MCP retrieval wrapper: cited JSON over stdio or Streamable HTTP.

Set SEARCH_ENDPOINT and SEARCH_API_KEY. KNOWLEDGE_BASE_NAME defaults to
kb-documents; set it to an empty string to deliberately use direct semantic
search. Service/authentication failures are errors, never silent fallbacks.
SEARCH_INDEX_SCHEMA is baseline or hybrid and controls the selected fields.
"""
import argparse
import json
import os
import re

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

SEARCH_API_VERSION = os.environ.get("SEARCH_API_VERSION", "2026-05-01-preview")
SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT", "").rstrip("/")
SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY", "")
SEARCH_INDEX_NAME = os.environ.get("SEARCH_INDEX_NAME", "idx-documents")
SEARCH_INDEX_SCHEMA = os.environ.get("SEARCH_INDEX_SCHEMA", "baseline")
KNOWLEDGE_BASE_NAME = os.environ.get("KNOWLEDGE_BASE_NAME", "kb-documents")
KNOWLEDGE_SOURCE_NAME = os.environ.get("KNOWLEDGE_SOURCE_NAME", "ks-documents")
SEMANTIC_CONFIG_NAME = os.environ.get("SEMANTIC_CONFIG_NAME", "semantic-config")
MCP_TOOL_NAME = os.environ.get("MCP_TOOL_NAME", "retrieve_documents")
MCP_TOOL_DESCRIPTION = os.environ.get(
    "MCP_TOOL_DESCRIPTION",
    "Search the indexed document corpus and return JSON with cited passages, "
    "source URIs, available page ranges, section labels and extraction provenance.",
)
MCP_SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "knowledge-base-wrapper")
MCP_ALLOWED_HOSTS = [value.strip() for value in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",")
                     if value.strip()]
mcp = FastMCP(
    MCP_SERVER_NAME,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]",
                       "127.0.0.1:*", "localhost:*", "[::1]:*", *MCP_ALLOWED_HOSTS],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    ),
)


def _validate_config() -> None:
    missing = [name for name, value in (
        ("SEARCH_ENDPOINT", SEARCH_ENDPOINT), ("SEARCH_API_KEY", SEARCH_API_KEY)
    ) if not value or "<" in value]
    if missing:
        raise ValueError("Configure " + ", ".join(missing) + " before retrieval.")
    if SEARCH_INDEX_SCHEMA not in {"baseline", "hybrid"}:
        raise ValueError("SEARCH_INDEX_SCHEMA must be baseline or hybrid.")


def _headers() -> dict:
    return {"api-key": SEARCH_API_KEY, "Content-Type": "application/json"}


def _format_heading(source: dict) -> str:
    trail = " > ".join(source[key] for key in ("sectionH1", "sectionH2", "sectionH3")
                       if source.get(key))
    return source.get("sectionLabel") or trail or "untitled section"


def _source_result(source: dict, reference_id=None) -> dict:
    start = source.get("pageNumberFrom", source.get("pageNumber"))
    end = source.get("pageNumberTo", start)
    title = source.get("sourceDocument") or source.get("title")
    uri = source.get("sourceUri") or source.get("url")
    result = {
        "id": reference_id if reference_id is not None else source.get("chunkId"),
        "text": source.get("content") or source.get("text") or "",
        "title": title, "url": uri, "sourceDocument": title, "sourceUri": uri,
        "sectionLabel": _format_heading(source),
        "pageNumberFrom": start, "pageNumberTo": end,
        "pageStart": start, "pageEnd": end,
        "extractionTier": source.get("extractionTier"),
        "contentKind": source.get("contentKind"),
        "pageReferenceBasis": "document",
    }
    part = re.search(r"^(.*)__p(\d+)-(\d+)\.pdf$", title or "", re.I)
    if part:
        result["pageReferenceBasis"] = "split-part"
        result["originalDocument"] = part[1] + ".pdf"
        offset = int(part[2]) - 1
        # A filename supplies an offset, never evidence of a chunk's page.
        if isinstance(start, int) and not isinstance(start, bool) and start > 0:
            result["originalPageNumberFrom"] = start + offset
        if isinstance(end, int) and not isinstance(end, bool) and end > 0:
            result["originalPageNumberTo"] = end + offset
    return result


def _encode(query: str, mode: str, results: list, answer: str = "",
            available_results: int | None = None, top_k: int | None = None) -> str:
    payload = {
        "query": query, "mode": mode, "answer": answer, "results": results,
        "message": "" if results or answer else "No matching content found in the indexed document corpus.",
    }
    if available_results is not None and available_results > len(results):
        payload["limit"] = {"requested": top_k, "returned": len(results), "available": available_results}
        payload["message"] = (
            f"Showing {len(results)} of {available_results} knowledge-base passages (top_k={top_k}). "
            "The full knowledge-base response was omitted because it can cite excluded passages."
        )
    return json.dumps(payload, ensure_ascii=False)


async def _retrieve_via_knowledge_base(query: str, top_k: int = 5) -> str | None:
    if not KNOWLEDGE_BASE_NAME:
        return None
    url = f"{SEARCH_ENDPOINT}/knowledgebases/{KNOWLEDGE_BASE_NAME}/retrieve?api-version={SEARCH_API_VERSION}"
    body = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": query}]}],
        "knowledgeSourceParams": [{
            "kind": "searchIndex", "knowledgeSourceName": KNOWLEDGE_SOURCE_NAME,
            "includeReferences": True, "includeReferenceSourceData": True,
        }],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, headers=_headers(), json=body)
        response.raise_for_status()
        data = response.json()
    answer = "\n".join(
        content.get("text", "")
        for item in (data.get("response") or [])
        for content in (item.get("content") or [])
        if isinstance(content.get("text"), str)
    )
    references = data.get("references") or []
    results = [
        _source_result(ref.get("sourceData") or {}, ref.get("id", ref.get("ref_id")))
        for ref in references[:top_k]
    ]
    if len(references) > top_k:
        answer = ""
    return _encode(query, "knowledge-base", results, answer, len(references), top_k)


async def _retrieve_via_direct_search(query: str, top_k: int) -> str:
    url = f"{SEARCH_ENDPOINT}/indexes('{SEARCH_INDEX_NAME}')/docs/search?api-version={SEARCH_API_VERSION}"
    fields = "chunkId,content,sourceDocument,sourceUri"
    fields += (
        ",sectionLabel,pageNumberFrom,pageNumberTo,extractionTier,contentKind"
        if SEARCH_INDEX_SCHEMA == "hybrid" else ",sectionH1,sectionH2,sectionH3"
    )
    body = {
        "search": query, "queryType": "semantic",
        "semanticConfiguration": SEMANTIC_CONFIG_NAME, "top": top_k, "select": fields,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, headers=_headers(), json=body)
        response.raise_for_status()
        results = response.json().get("value", [])
    return _encode(query, "direct-search", [_source_result(doc) for doc in results])


async def retrieve_documents(query: str, top_k: int = 5) -> str:
    """Return cited JSON. Direct search is opt-in by disabling the knowledge base."""
    _validate_config()
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must not be empty.")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 50:
        raise ValueError("top_k must be an integer between 1 and 50.")
    result = await _retrieve_via_knowledge_base(query, top_k)
    return result if result is not None else await _retrieve_via_direct_search(query, top_k)


mcp.add_tool(retrieve_documents, name=MCP_TOOL_NAME, description=MCP_TOOL_DESCRIPTION)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"],
                        default=os.environ.get("MCP_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    _validate_config()
    if args.transport == "streamable-http" and args.host not in {"127.0.0.1", "localhost", "::1"} and not MCP_ALLOWED_HOSTS:
        parser.error("Set MCP_ALLOWED_HOSTS to the deployed hostname(s) before binding a non-loopback host.")
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
