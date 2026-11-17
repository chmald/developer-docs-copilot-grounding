[Demo README](../README.md) › Docs › 06 — MCP endpoint and custom wrapper server

# 06 — MCP endpoint and custom wrapper server

<p>
  <img src="./assets/icons/ai-search.svg" width="48" alt="Azure AI Search"/>&nbsp;
  <img src="./assets/icons/container-apps.svg" width="48" alt="Azure Container Apps"/>&nbsp;
  <img src="./assets/icons/container-registry.svg" width="48" alt="Azure Container Registry"/>&nbsp;
  <img src="./assets/icons/key-vault.svg" width="48" alt="Azure Key Vault"/>&nbsp;
  <img src="./assets/icons/code.svg" width="48" alt="Code"/>
</p>

![Version](./assets/badges/version.svg) ![Public preview](./assets/badges/public-preview.svg) ![Search API 2026-05-01-preview](./assets/badges/search-api.svg) ![Default](./assets/badges/default.svg) ![Optional](./assets/badges/optional.svg)

> Deep dive on the pattern's Knowledge Base retrieval surface: how the native MCP endpoint works, how to check it's enabled on your Search service, and how the optional custom MCP server wrapper adds defense-in-depth on top of it. Referenced from [03-deployment.md Phase 3-4](03-deployment.md#phase-3--knowledge-base--mcp-endpoint) and [docs/00-reproduce-this-demo.md Parts C-D](00-reproduce-this-demo.md) — read this before running those phases if anything is unclear.

## At a glance

| | |
|---|---|
| **Goal** | Expose the Knowledge Base to any MCP client — natively, or through the optional wrapper |
| **Default path** | ![Default](./assets/badges/default.svg) Native Knowledge Base MCP endpoint on Azure AI Search (public preview, API `2026-05-01-preview`) |
| **Optional path** | ![Optional](./assets/badges/optional.svg) Custom wrapper server on Azure Container Apps (`scripts/mcp_fallback_server.py`) |
| **Sections** | [§ 3 check availability](#3--checking-availability) · [§ 4 wrapper](#4--the-optional-custom-wrapper-server) · [§ 5 deploy](#5--deploying-the-custom-wrapper-server-to-azure-container-apps) · [Verification](#verification) |

> [!NOTE]
> The Knowledge Base and its native MCP endpoint are **public preview**. The wrapper is **optional** — a working demo does not need it.

## Native endpoint vs wrapper

[![Native MCP endpoint versus custom wrapper server](./assets/mcp-endpoint-vs-wrapper.png)](./assets/mcp-endpoint-vs-wrapper.png)

<sub>Editable source: [`assets/mcp-endpoint-vs-wrapper.drawio`](./assets/mcp-endpoint-vs-wrapper.drawio) - regenerate with `python scripts/export_diagrams.py docs/assets`.</sub>

| | Native MCP endpoint | Custom wrapper server |
|---|---|---|
| **Status** | ![Default](./assets/badges/default.svg) ![Public preview](./assets/badges/public-preview.svg) | ![Optional](./assets/badges/optional.svg) |
| **Hosted by** | <img src="./assets/icons/ai-search.svg" width="16" alt=""/> Azure AI Search (`/knowledgebases/{name}/mcp`) | <img src="./assets/icons/container-apps.svg" width="16" alt=""/> Azure Container Apps (`scripts/mcp_fallback_server.py`) |
| **Custom code** | None | Small Python `FastMCP` server |
| **Why choose it** | Zero custom code; any MCP client can call it | Configurable tool identity, consistent JSON citations, local/browser integration and an extension point for custom processing |
| **Retrieval mode** | Knowledge Base retrieval | Knowledge Base `retrieve` REST by default; direct semantic index search only when `KNOWLEDGE_BASE_NAME` is explicitly empty. Errors never silently change mode |
| **Recommendation** | ![Default](./assets/badges/default.svg) Use this unless § 3 reports `wrapper-required` | ![Optional](./assets/badges/optional.svg) Deploy only when you need the extras above, or the native surface isn't enabled on your service/region/API version |

---

## <img src="./assets/icons/resource-group.svg" width="28" alt=""/> 1 — Prerequisites checklist

Work through these in order:

| Step | | Prerequisite | Gate |
|---|---|---|---|
| 1 | <img src="./assets/icons/ai-search.svg" width="24" alt=""/> | [Index populated](#1-index-populated) | ☐ `idx-documents-hybrid` has documents |
| 2 | <img src="./assets/icons/foundry-models.svg" width="24" alt=""/> | [Chat model deployed](#2-chat-model-deployed) | ☐ `chat` deployment exists |

### 1. Index populated

`idx-documents-hybrid` must have documents ([03-deployment.md § Phase 2](03-deployment.md#phase-2--hybrid-ingestion) complete) before a Knowledge Base is useful to create.

### 2. Chat model deployed

The Foundry account must have a chat-completion deployment (`chat`, e.g. `gpt-5-mini`) for the Knowledge Base's query planning — provisioned by `infra/modules/foundry.bicep` in Phase 1.

---

## <img src="./assets/icons/ai-search.svg" width="28" alt=""/> 2 — Understanding the native MCP endpoint

Azure AI Search's **Knowledge Bases** (agentic retrieval) can expose their `retrieve` operation directly as an MCP server — a Streamable HTTP endpoint implementing the Model Context Protocol's JSON-RPC 2.0 transport, so any MCP client can call it with zero custom code.

**Conceptual shape** (verify exact path/API version at deployment time — see § 3):

```
POST https://{search-service}.search.windows.net/knowledgebases/{knowledgeBaseName}/mcp?api-version={api-version}
Authorization: Bearer {entra-id-token-scoped-to-https://search.azure.com/.default}     # recommended; api-key also accepted
Content-Type: application/json
```

The endpoint speaks MCP's standard lifecycle (`initialize`, `tools/list`, `tools/call`) and exposes a single tool that performs agentic retrieval against the configured index(es), returning grounded text with per-chunk citations.

> [!IMPORTANT]
> **This is a real, documented Azure AI Search capability** — Microsoft Learn states: *"each knowledge base is a standalone MCP server that exposes the `knowledge_base_retrieve` tool. Any MCP-compatible client, including Foundry Agent Service, GitHub Copilot, Claude, and Cursor, can invoke this tool."* The exact API version and regional rollout still move quickly (agentic retrieval was renamed from "Knowledge Agents" to "Knowledge Bases" in 2026), so always re-verify against the current Azure AI Search REST API reference before a production build — but do not treat the endpoint itself as speculative.

## <img src="./assets/icons/monitor.svg" width="28" alt=""/> 3 — Checking availability

`post_deploy_search.py --check-mcp-endpoint` (see [scripts/post_deploy_search.py](../scripts/post_deploy_search.py)) does the following:

1. Sends an MCP `initialize` request to `POST {searchEndpoint}/knowledgebases/{knowledgeBaseName}/mcp?api-version=2026-05-01-preview`
2. If it receives a valid MCP `initialize` response → records `mcpEndpointAvailability: "native"` in `demo-ids.local.json`
3. If it receives a 404, `FeatureNotEnabled`, or an unrecognized-path error → records `mcpEndpointAvailability: "wrapper-required"` and prints the custom wrapper deployment instructions

```bash
python post_deploy_search.py --ids-file ../demo-ids.local.json --check-mcp-endpoint
```

If you get an ambiguous result, cross-check manually:

```bash
# Confirm the Knowledge Base itself exists and responds to a direct retrieve call
python post_deploy_search.py --ids-file ../demo-ids.local.json --test-retrieve --query "test question your corpus can answer"
```

If the direct `retrieve` call works but the `/mcp` path 404s, the Knowledge Base is fine — only the MCP surface specifically isn't enabled yet on your service/region/API version. This is exactly the case the optional custom wrapper server exists for.

## <img src="./assets/icons/container-apps.svg" width="28" alt=""/> 4 — The optional custom wrapper server

![Optional](./assets/badges/optional.svg) Not required for a working demo.

`scripts/mcp_fallback_server.py` is a small Python MCP server (built on the official `mcp` SDK's `FastMCP` helper). It is **not required** for the native Copilot path. Its configurable tool accepts `query` / `top_k` and returns cited JSON; this is **not the native `knowledge_base_retrieve` argument schema**. The optional [browser app](../frontend/README.md) uses this wrapper contract. Network controls, user authentication and automatic token refresh are not supplied by the tool itself.

<details><summary><b>Show the tool registration excerpt</b></summary>

```python
async def retrieve_documents(query: str, top_k: int = 5) -> str:
    """Search the indexed document corpus and return grounded, cited results."""

# Registered with a configurable name/description so one server serves any corpus:
mcp.add_tool(retrieve_documents, name=MCP_TOOL_NAME, description=MCP_TOOL_DESCRIPTION)
```

</details>

`MCP_TOOL_NAME` / `MCP_TOOL_DESCRIPTION` come from the `corpus` block in `demo-ids.local.json` (passed through as Container App env vars by `deploy_mcp_server.ps1`).

> [!TIP]
> **Set the description to reflect the actual corpus** — GitHub Copilot decides whether to invoke the tool from that text, so a generic description gets skipped for domain questions. See [01-architecture.md § Adapting this pattern to another document corpus](01-architecture.md#adapting-this-pattern-to-another-document-corpus).

Internally, it:

| Order | Path | Detail |
|---|---|---|
| 1 | <img src="./assets/icons/ai-search.svg" width="16" alt=""/> Knowledge Base `retrieve` | Direct `POST /knowledgebases/{name}/retrieve?api-version=...`; authentication, API and service failures are surfaced, not silently bypassed |
| 2 | <img src="./assets/icons/ai-search.svg" width="16" alt=""/> Explicit direct-index mode | Set `KNOWLEDGE_BASE_NAME` empty to select semantic search. Set `SEARCH_INDEX_NAME` and `SEARCH_INDEX_SCHEMA=hybrid` for hybrid index metadata; this path does not generate query vectors |
| 3 | <img src="./assets/icons/code.svg" width="16" alt=""/> Formatting | Cited JSON carries source URI/document, available page range, section, content kind and extraction tier. Missing metadata stays missing; split-part offsets are reported separately |

Choose retrieval mode deliberately. The wrapper does not mask a broken KB, infer missing
citations, or certify schematic output. See [runtime configuration](./12-configuration-reference.md#4--process-environment-variables-runtime).
For headings, an existing hybrid `sectionLabel` takes precedence; baseline records retain
their legacy `sectionH1 > sectionH2 > sectionH3` chain.

Run it locally over stdio for single-developer testing:

```bash
cd scripts
python mcp_fallback_server.py --transport stdio
```

Or over Streamable HTTP (what Container Apps hosting uses):

```bash
python mcp_fallback_server.py --transport streamable-http --host 127.0.0.1 --port 8080
```

## <img src="./assets/icons/container-registry.svg" width="28" alt=""/> 5 — Deploying the custom wrapper server to Azure Container Apps

```bash
cd scripts
./deploy_mcp_server.ps1 -IdsFile ../demo-ids.local.json
```

For the hybrid index, supply `-IndexSchema hybrid` to select the suffixed hybrid index and
`kb-hybrid` / `ks-hybrid`; add `-DirectSearch` only when
deliberately selecting direct-index retrieval instead of the configured knowledge base.
The deployment script verifies the local tenant/subscription configuration and supplies
the intended source/index/schema and allowed host values.

This script:

| Step | | Action |
|---|---|---|
| 1 | <img src="./assets/icons/container-registry.svg" width="24" alt=""/> | Builds the container image from `scripts/` via `az acr build` (no local Docker required) |
| 2 | <img src="./assets/icons/container-apps.svg" width="24" alt=""/> | Deploys/updates the Container App in the environment provisioned by `infra/modules/containerapp.bicep` (only created when `deploy.ps1 -DeployFallbackServer` was run in Phase 1/Part D) |
| 3 | <img src="./assets/icons/key-vault.svg" width="24" alt=""/> | Wires the Search endpoint + Key Vault reference for the admin/query key as Container App secrets — never as plaintext env vars |
| 4 | <img src="./assets/icons/file.svg" width="24" alt=""/> | Writes the resulting Container App FQDN into `demo-ids.local.json` |

## <img src="./assets/icons/application-insights.svg" width="28" alt=""/> Verification

| Step | Expected result |
|---|---|
| Connect an MCP client from [docs/07](./07-github-copilot-mcp-client-setup.md), or use the [browser app's remote MCP mode](../frontend/README.md#optional-remote-mcp--native-knowledge-endpoint) | Complete `initialize`, initialized notification and tool discovery before calling tools |
| Discover and call your configured wrapper tool | `query` / `top_k` call returns source-bearing JSON; verify document/page evidence against the PDF |
| Test an invalid credential or KB name | Visible service/tool error, not an apparently successful direct-index fallback |

A bare `tools/list` POST without MCP initialization is not a valid protocol health check.
If verification fails, see [05-troubleshooting.md § 4](05-troubleshooting.md#4--mcp-endpoint-native-or-fallback).

## <img src="./assets/icons/alerts.svg" width="28" alt=""/> Troubleshooting

| Where | Symptom | Root cause | Fix |
|---|---|---|---|
| <img src="./assets/icons/ai-search.svg" width="16" alt=""/> Search | `/mcp` path returns 404 on the native endpoint | MCP surface not enabled on this Search service/tier/region/API version | Deploy the optional custom wrapper; periodically re-check native availability |
| <img src="./assets/icons/container-apps.svg" width="16" alt=""/> Wrapper | Custom wrapper's Knowledge Base call fails with 401 | Search admin key rotated or Key Vault reference misconfigured | Re-check the Container App's Key Vault secret reference and the key's current value |
| <img src="./assets/icons/container-apps.svg" width="16" alt=""/> Wrapper | Direct-index results appear instead of KB results | `KNOWLEDGE_BASE_NAME` is explicitly empty | Configure the intended KB/source names; failures now remain visible rather than triggering fallback |
| <img src="./assets/icons/ai-search.svg" width="16" alt=""/> Search | Both native and wrapper return different citations for the same question | One is querying a stale index snapshot or a differently-configured Knowledge Base | Confirm both point at the same `searchIndexName` / `knowledgeBaseName`; re-run the indexer if the corpus changed recently |

---

Next: [07 - GitHub Copilot MCP client setup](./07-github-copilot-mcp-client-setup.md) →

*Last updated: 2026-11-02*
