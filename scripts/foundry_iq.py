"""Shared Foundry IQ response parsing and tenant-guarded local authentication."""
import json
import shutil
import subprocess
from urllib.parse import urlparse
from uuid import UUID


def project_endpoint(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".services.ai.azure.com")
            or parsed.port not in {None, 443} or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or not parsed.path.startswith("/api/projects/")
            or len(parsed.path.rstrip("/").split("/")) != 4
            or not parsed.path.rstrip("/").split("/")[-1]):
        raise ValueError("Use the Microsoft Foundry project HTTPS endpoint /api/projects/<project>.")
    return value.rstrip("/")


def cli_context(tenant: str, subscription: str, timeout: float = 60) -> str:
    tenant, subscription = str(UUID(tenant)), str(UUID(subscription))
    az = shutil.which("az")
    if not az:
        raise ValueError("Azure CLI is required for local Foundry authentication.")
    result = subprocess.run(
        [az, "account", "show", "--query", "{tenant:tenantId,subscription:id}", "-o", "json"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=True,
    )
    context = json.loads(result.stdout)
    if (str(UUID(context["tenant"])) != tenant
            or str(UUID(context["subscription"])) != subscription):
        raise ValueError("Azure CLI tenant/subscription does not match the configured demo target.")
    return az


def cli_token(tenant: str, subscription: str, resource: str, timeout: float = 60) -> str:
    az = cli_context(tenant, subscription, timeout)
    result = subprocess.run(
        [az, "account", "get-access-token", "--tenant", tenant, "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=True,
    )
    token = result.stdout.strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("Azure CLI did not return an access token.")
    return token


def response_evidence(data: dict) -> dict:
    if data.get("status") != "completed" or data.get("error"):
        raise ValueError("The Foundry agent did not complete the response.")
    answer, citations, seen = [], [], set()
    for item in data.get("output", []):
        if item.get("type") == "mcp_approval_request":
            raise ValueError("The retrieval tool requires approval; check the agent configuration.")
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        for content in item.get("content", []):
            if content.get("type") != "output_text":
                continue
            if isinstance(content.get("text"), str):
                answer.append(content["text"])
            for annotation in content.get("annotations", []):
                if annotation.get("type") != "url_citation":
                    continue
                url = annotation.get("url", "")
                title = annotation.get("title") or url
                if not isinstance(url, str) or not isinstance(title, str):
                    continue
                key = (title, url)
                if key not in seen:
                    citations.append({"title": title, "uri": url, "metadata": annotation})
                    seen.add(key)
    text = "\n\n".join(answer).strip()
    if not text:
        raise ValueError("The Foundry agent returned no answer.")
    return {
        "answer": text, "text": "", "citations": citations, "raw": {},
        "notice": (
            "Citations are preserved from the agent. Source links retain their original access requirements."
            if citations else
            "No structured source links were returned. Inspect the answer's references; source URLs and pages are not inferred."
        ),
    }
