# Document Grounding Studio

A local browser client for document Q&A through **Foundry IQ and a Foundry prompt
agent**. The gateway calls the project's Responses API; it does not start a local
MCP server or call a separate answer model on the default Q&A path. The browser
never receives credentials or chooses the project, agent or retrieval tool.

## Default - Foundry IQ Q&A

Follow [13 - Foundry IQ Q&A](../docs/13-foundry-iq-qa.md) to configure the existing
knowledge base and project connection. With the updated dependencies installed:

```powershell
python scripts\configure_foundry_iq.py --ids-file demo-ids.local.json --knowledge-base kb-hybrid
.\scripts\start_qa.ps1 -Python <path-to-configured-python.exe>
```

Open `http://127.0.0.1:8000`. Answers are the default, follow-ups use the prior
response ID, and **New conversation** clears the local turn chain. The gateway
binds follow-up IDs to an HttpOnly browser session. Returned URL annotations
become source cards; textual references remain in the answer and missing source
URLs/pages are never inferred. Responses display when complete, not token-by-token.
**Inspect retrieval (diagnostic)** remains available with the separate legacy
Search/MCP configuration below.

The default needs only server-side `FOUNDRY_PROJECT_ENDPOINT`,
`FOUNDRY_AGENT_NAME`, `FOUNDRY_AUTH_MODE=azure-cli`, `FOUNDRY_TENANT_ID` and
`FOUNDRY_SUBSCRIPTION_ID`; the launcher loads these from local IDs. For an Azure
host with agent-invocation permission, `FOUNDRY_AUTH_MODE=managed-identity` avoids
an Azure CLI dependency. Hosting the app still requires a separate deployment and
end-user authentication; Foundry supplies the agent API, not a public chat website.

## Legacy and diagnostic configuration (PowerShell, repository root)

Use Python 3.11+ (3.12 recommended). Create a **separate environment** to keep
the gateway isolated from other local tools. The frontend and backend pin the
same modern MCP SDK with Streamable HTTP support. The frontend's dependencies
also run the existing stdio backend (`httpx` + MCP).

```powershell
python -m venv frontend\.venv
& frontend\.venv\Scripts\python.exe -m pip install -r frontend\requirements.txt

# Supply your existing deployed corpus configuration in this process.
$env:QA_BACKEND = 'legacy' # Explicit opt-in; omit for Foundry-agent Q&A.
$env:SEARCH_ENDPOINT = 'https://<your-search-service>.search.windows.net'
$env:SEARCH_API_KEY = '<your-local-secret>'
$env:SEARCH_INDEX_NAME = '<your-index>'
# Match the deployed index: hybrid for the optional schematic/DI+CU schema;
# use baseline for the original Document Intelligence index.
$env:SEARCH_INDEX_SCHEMA = 'hybrid'
$env:KNOWLEDGE_BASE_NAME = '<your-existing-knowledge-base>'
$env:KNOWLEDGE_SOURCE_NAME = '<your-existing-knowledge-source>'

& frontend\.venv\Scripts\python.exe -m frontend.app
```

Open **http://127.0.0.1:8000**. By default, each request launches the existing
`scripts\mcp_fallback_server.py --transport stdio` with the gateway's Python and
environment, initializes an MCP session, discovers the allowlisted tool and calls
`retrieve_documents(query, top_k)`. Sessions/processes are closed after each request.
The UI shows returned source citations, full passages, metadata and raw retrieval.
A new question replaces the previous result; grounded questions are independent
turns, not a persisted chat history.

On Windows ARM64, if a dependency does not provide a native wheel, use an x64
Python 3.12 interpreter under Windows emulation in a separate environment. For
example, `uv venv --python <path-to-x64-python.exe> frontend\.runtime-x64\.venv`
followed by `uv pip install --python frontend\.runtime-x64\.venv\Scripts\python.exe -r frontend\requirements.txt`.
Run the app with that environment's Python; do not overwrite an existing `.venv`.

The backend owns Search, knowledge-base/Foundry IQ retrieval and fallback behavior.
Set `KNOWLEDGE_BASE_NAME` to an empty string only if intentionally selecting the
backend's direct-index path. This UI neither provisions Foundry IQ nor claims that
the configured endpoint uses a particular retrieval path.

### Legacy grounded answers

Configure an **already deployed**, chat-completions-compatible Azure OpenAI model.
There is deliberately no default model name or capability claim. The v1 endpoint
is a base URL ending in `/openai/v1/`; no dated `api-version` is appended.

```powershell
$env:AZURE_OPENAI_V1_ENDPOINT = 'https://<your-resource>.openai.azure.com/openai/v1/'
$env:AZURE_OPENAI_API_KEY = '<your-local-secret>'
$env:AZURE_OPENAI_DEPLOYMENT = '<your-actual-deployment-name>'
& frontend\.venv\Scripts\python.exe -m frontend.app
```

For a resource with local authentication disabled, use an Azure CLI login instead:

```powershell
# Use the CLI profile containing the intended login, if not the default.
# $env:AZURE_CONFIG_DIR = '<path-to-your-azure-cli-profile>'
$env:AZURE_OPENAI_AUTH_MODE = 'azure-cli'
$env:AZURE_OPENAI_TENANT_ID = '<tenant-guid>'
$env:AZURE_OPENAI_SUBSCRIPTION_ID = '<subscription-guid>'
az account show --query "{tenant:tenantId, subscription:id, user:user.name}" -o table
# If the context differs, sign in with --tenant and select the intended subscription.
& frontend\.venv\Scripts\python.exe -m frontend.app
```

In this mode the gateway checks the active tenant **and** subscription before
each token acquisition, requests a fresh tenant-scoped Cognitive Services token
through the Azure CLI, and refuses mismatched contexts. The token stays on the
server and is sent only to supported Microsoft Azure OpenAI/Foundry global-cloud
endpoints. Use the same `AZURE_CONFIG_DIR` when refreshing the CLI login. Azure
CLI mode requires the CLI on the gateway's PATH; the default container image
does not include it. API-key mode remains the default.

Choose **Grounded answer** in the UI. Every question first retrieves evidence,
then sends that evidence and the question to the configured model. Documents are
marked as untrusted evidence in the instruction; this reduces but cannot eliminate
prompt-injection or hallucination risk. Verify answers against source passages.
Unsupported deployments and model/auth errors are visible; a failed model call
preserves retrieved evidence and never substitutes a fake answer.

### Optional remote MCP / native knowledge endpoint

Server configuration only, set before startup:

```powershell
$env:FRONTEND_MCP_TRANSPORT = 'streamable-http'
$env:FRONTEND_MCP_URL = 'https://<your-server>/mcp'
$env:FRONTEND_MCP_BEARER_TOKEN = '<optional-server-side-token>'
$env:MCP_TOOL_NAME = 'retrieve_documents'
& frontend\.venv\Scripts\python.exe -m frontend.app
```

The remote server must advertise the configured tool with a compatible
`query: string, top_k: integer` schema. Native knowledge endpoints with different
tool schemas need a compatible wrapper (such as the existing backend); merely
changing the URL does not translate tool arguments. Tokens must be supplied
externally and refreshed by the operator. Remote HTTP without TLS is rejected;
plain HTTP is permitted only for literal loopback addresses.

### Configuration reference

| Environment variable | Default / behavior |
|---|---|
| `FRONTEND_HOST` | `127.0.0.1` (local only) |
| `FRONTEND_PORT` | `8000` |
| `FRONTEND_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]`; comma-separated trusted hostnames |
| `FRONTEND_MCP_TRANSPORT` | `stdio` or `streamable-http` |
| `FRONTEND_MCP_COMMAND` | Optional JSON argv array, e.g. `["python","path-to-compatible-server.py"]`; trusted operator only |
| `FRONTEND_MCP_URL` | Required for Streamable HTTP; never returned to browser |
| `FRONTEND_MCP_BEARER_TOKEN` | Optional server-side HTTP bearer token |
| `MCP_TOOL_NAME` | `retrieve_documents`; single-tool allowlist, shared with default backend |
| `SEARCH_INDEX_SCHEMA` | Backend default `baseline`; set `hybrid` for the DI/CU hybrid index so direct retrieval selects the correct citation/provenance fields |
| `FRONTEND_TIMEOUT_SECONDS` | `60`; accepted range `1`–`300` per retrieval/model call |
| `AZURE_OPENAI_V1_ENDPOINT` | Optional HTTPS v1 base URL |
| `AZURE_OPENAI_DEPLOYMENT` | Required with endpoint and authentication to enable grounded answers |
| `AZURE_OPENAI_AUTH_MODE` | `api-key` (default) or `azure-cli` |
| `AZURE_OPENAI_API_KEY` | Server-only key for `api-key` mode |
| `AZURE_OPENAI_TENANT_ID` | Required tenant GUID for `azure-cli` mode |
| `AZURE_OPENAI_SUBSCRIPTION_ID` | Required subscription GUID for `azure-cli` mode |

Other Search/backend variables are inherited unchanged. Configuration is read at
startup; restart after changing it. The public config route returns the tool,
transport, result limits and whether chat is configured, **not an upstream connectivity check**.
The first retrieval verifies connectivity. Query length is limited to 4000
characters, top_k to 1–5 (default 3), request bodies to 20 KB, retrieval payloads to 180,000
characters, and concurrent upstream operations to four. Large responses fail
visibly rather than silently dropping citations. No browser endpoint can modify
server configuration.

The default wrapper applies `top_k` to returned Knowledge Base passages before
serializing the MCP result, not just to direct-index search. When limiting
passages, it omits the full Knowledge Base response, which can include duplicated
evidence or references to excluded passages, and displays a notice with the
returned/available counts. This bounds the returned evidence; it does not limit
the Knowledge Base's internal query planning.

### DI / CU comparison and citations

The gateway accepts text-only MCP results, JSON encoded as text, and modern
`structuredContent`. It retains the complete raw result and surfaces
`sourceDocument` / `sourceUri`, section labels, page ranges, `extractionTier`,
`contentKind`, and any additional returned provenance without rewriting them.
Text headings and `Sources:` bullets are recognized for the legacy backend.
Missing metadata is not inferred. No image, diagram or local file is fetched or
executed by the UI. This frontend **does not ingest documents or run DI/CU**;
use the repository ingestion and extraction-comparison scripts, then query the
resulting index. Source links are opened directly by the browser, not proxied
through the gateway; links to private Storage require the browser's own network
access and authorization. Retrieved passage HTML is rebuilt using only passive
formatting elements (including tables, headings and lists); scripts, embedded
resources, links and source attributes are not activated. Model answers, source
metadata and raw MCP output remain plain text.

## Container (build from repository root)

```powershell
docker build -f frontend\Dockerfile -t developer-docs-ui .
# Create frontend\.env locally with KEY=value entries for the variables above.
# Do not commit it. The Dockerfile-specific ignore file excludes secrets,
# virtual environments, corpus files and all unnecessary build context.
docker run --rm --name developer-docs-ui -p 127.0.0.1:8000:8000 --env-file frontend\.env developer-docs-ui
```

The image includes the existing backend script and runs as an unprivileged user.
Only the process inside the container binds `0.0.0.0`; publishing on
`127.0.0.1` keeps the browser service local. There are no embedded credentials.
An env file is **not automatically loaded by local Python**; set process
environment variables instead. Docker injects `--env-file` values.

**Do not expose this service publicly as-is.** It has no user authentication or
authorization and can access your corpus and consume model quota. Before any
non-loopback deployment, add an authenticating reverse proxy / platform auth,
per-user authorization for the corpus and model, TLS, rate limits and request
auditing; configure `FRONTEND_ALLOWED_HOSTS` for the intended host. Same-origin
requests, host validation and CSP are defense-in-depth, not authentication.
Do not use a wildcard trusted-host configuration. Proxy TLS termination must
forward a trusted, accurate scheme/Host so the same-origin check remains correct;
never trust forwarded headers from arbitrary clients. Deployment is intentionally
not automated. Prompts/evidence leave your machine only for the configured
MCP/Search/model services; ensure those destinations meet your data policy.

## Validate without cloud access

```powershell
& frontend\.venv\Scripts\python.exe -m pytest frontend\tests -q
node --check frontend\static\app.js
```

Tests launch a harmless local FastMCP fixture and exercise real **stdio and
Streamable HTTP** protocol round-trips, legacy text/structured citations,
tool/error/timeout handling, origin and input restrictions, and optional model
configuration, including CLI context mismatches and token redaction. The v1
model and Azure CLI calls are mocked; no cloud keys or services are needed.
Docker runtime validation requires Docker to be installed separately.

With the local gateway running, Node.js 22+ and Microsoft Edge (or a Chromium
executable specified by `FRONTEND_TEST_BROWSER`), run the passage-rendering
browser regression without invoking Search or the model:

```powershell
node --test frontend\tests\test_passage_rendering.mjs
```

It verifies table formatting, plain-text preservation, and rejection of active
content, source attributes and embedded-resource requests.
