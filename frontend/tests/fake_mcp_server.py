"""Cloud-free protocol fixture; never imported by the production gateway."""
import asyncio
import json
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

server = FastMCP("harmless-frontend-test")


@server.tool()
async def retrieve_documents(query: str, top_k: int = 5) -> CallToolResult:
    if query == "tool-error":
        return CallToolResult(isError=True, content=[TextContent(type="text", text="fixture tool failed")])
    if query == "slow":
        await asyncio.sleep(10)
    if query == "text":
        return CallToolResult(content=[TextContent(type="text", text="[example.md — Initialization]\nEvidence only.")])
    if query == "json-text":
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({
            "message": "Showing 1 of 8 knowledge-base passages.",
            "mode": "knowledge-base", "results": [{
                "title": "Developer guide", "url": "https://example.com/guide",
                "text": "Initialize the interface before routing signals.",
                "extractionTier": "di", "pageReferenceBasis": "split-part",
                "pageNumberFrom": 2,
            }],
        }))])
    document = {
        "sourceDocument": "example-schematic.pdf",
        "sourceUri": "https://example.com/doc",
        "sectionLabel": "Signal routing",
        "pageNumberFrom": 3,
        "pageNumberTo": 4,
        "extractionTier": "cu",
        "contentKind": "diagram",
        "content": f"Harmless evidence for {query}; top_k={top_k}.",
    }
    return CallToolResult(
        content=[TextContent(type="text", text=document["content"])],
        structuredContent={"documents": [document]},
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        server.settings.host = "127.0.0.1"
        server.settings.port = int(sys.argv[1])
        server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")
