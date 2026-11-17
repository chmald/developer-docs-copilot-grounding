"use strict";
const $ = (id) => document.getElementById(id);
let busy = false;
let backend = "foundry";
let previousResponseId = null;
const turns = [];

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
}

function renderPassages(text) {
  const host = $("passages");
  host.replaceChildren();
  if (!/<\/?(?:table|p|div|br|h[1-6]|ul|ol|li|strong|em|b|i|span|pre|code|blockquote)\b/i.test(text)) {
    host.textContent = text;
    return;
  }
  // Parse inertly, then rebuild only passive elements without source attributes.
  const template = document.createElement("template");
  template.innerHTML = text;
  const allowed = new Set(["table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "p", "div", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "strong", "em", "b", "i", "span", "pre", "code", "blockquote", "sup", "sub"]);
  const blocked = new Set(["script", "style", "iframe", "object", "embed", "svg", "math",
    "template", "noscript", "img", "audio", "video", "source", "link", "meta", "base"]);
  function appendSafe(node, parent) {
    if (node.nodeType === Node.TEXT_NODE) {
      parent.append(document.createTextNode(node.textContent));
    } else if (node.nodeType === Node.ELEMENT_NODE && !blocked.has(node.localName)) {
      const target = allowed.has(node.localName) ? document.createElement(node.localName) : parent;
      if (target !== parent) {
        if (["td", "th"].includes(node.localName)) {
          for (const attr of ["colspan", "rowspan"]) {
            const value = node.getAttribute(attr);
            if (/^[1-9]\d?$/.test(value || "")) target.setAttribute(attr, value);
          }
        }
        parent.append(target);
      }
      for (const child of node.childNodes) appendSafe(child, target);
    }
  }
  for (const node of template.content.childNodes) appendSafe(node, host);
}

function renderEvidence(data) {
  $("output").hidden = false;
  $("empty").hidden = true;
  $("answer-section").hidden = !data.answer;
  $("answer").textContent = data.answer || "";
  renderPassages(data.text || "No text passages returned. Inspect the structured MCP response below.");
  $("retrieval-notice").textContent = data.notice || "";
  $("retrieval-notice").hidden = !data.notice;
  $("raw").textContent = JSON.stringify(data.raw, null, 2);
  $("passages-section").hidden = !data.text;
  $("raw-section").hidden = !data.raw || Object.keys(data.raw).length === 0;
  const citations = data.citations || [];
  $("citation-count").textContent = `(${citations.length})`;
  $("result-count").textContent = `${citations.length} SOURCES`;
  $("citations").replaceChildren();
  if (!citations.length) {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = "No structured or recognizable textual citations returned. Check the original response; missing provenance is not inferred.";
    $("citations").append(note);
  }
  citations.forEach((citation, index) => {
    const card = document.createElement("article");
    card.className = "citation";
    const title = document.createElement("div");
    title.className = "citation-title";
    title.textContent = `${index + 1}. ${citation.title}`;
    card.append(title);
    try {
      const url = new URL(citation.uri);
      if (["https:", "http:"].includes(url.protocol) && !url.username && !url.password) {
        const link = document.createElement("a");
        link.textContent = "Open source ↗";
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "hint";
        card.append(link);
      }
    } catch { /* Missing or non-web source URIs remain visible in raw evidence. */ }
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Source metadata & extraction provenance";
    const metadata = document.createElement("pre");
    metadata.textContent = JSON.stringify(citation.metadata, null, 2);
    details.append(summary, metadata);
    card.append(details);
    $("citations").append(card);
  });
}

function updateMode() {
  const chat = $("mode").value === "chat";
  $("submit").textContent = chat ? "Ask your documents ↗" : "Inspect retrieval ↗";
  $("top-k-option").hidden = chat && backend === "foundry";
}
$("mode").addEventListener("change", updateMode);
$("new-conversation").addEventListener("click", () => {
  previousResponseId = null;
  turns.length = 0;
  $("history").replaceChildren();
  $("history-section").hidden = true;
  $("output").hidden = true;
  $("empty").hidden = false;
  $("error").hidden = true;
  $("query").value = "";
  $("status").textContent = "New conversation. Previous turns will not be sent.";
  $("result-count").textContent = "READY";
  $("query").focus();
});
document.querySelectorAll(".suggestion").forEach((button) => {
  button.addEventListener("click", () => {
    $("query").value = button.dataset.query;
    $("query").focus();
  });
});
$("question-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  const query = $("query").value.trim();
  if (!query) { $("query").focus(); return; }
  busy = true;
  $("submit").disabled = true;
  $("mode").disabled = true;
  $("new-conversation").disabled = true;
  $("error").hidden = true;
  $("output").hidden = true;
  $("empty").hidden = true;
  $("result-count").textContent = "WORKING";
  const chat = $("mode").value === "chat";
  const payload = {query, top_k: Number($("top-k").value)};
  if (chat && backend === "foundry" && previousResponseId) payload.previous_response_id = previousResponseId;
  $("status").textContent = chat ? "Your agent is retrieving evidence and preparing an answer…" : "Calling the diagnostic MCP retrieval tool…";
  document.querySelector(".results").setAttribute("aria-busy", "true");
  const start = performance.now();
  try {
    const response = await fetch(`/api/${$("mode").value}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      if (data.retrieval) renderEvidence(data.retrieval);
      throw new Error(data.error?.message || `Gateway request failed (${response.status}).`);
    }
    renderEvidence(data);
    if (chat && backend === "foundry") {
      $("history").replaceChildren();
      for (const turn of turns) {
        const entry = document.createElement("p");
        entry.textContent = `You: ${turn.query}\n\nAgent: ${turn.answer}`;
        $("history").append(entry);
      }
      $("history-section").hidden = turns.length === 0;
      turns.push({query, answer: data.answer || ""});
      previousResponseId = data.response_id;
    }
    $("status").textContent = `Completed in ${((performance.now() - start) / 1000).toFixed(1)}s · ${chat && backend === "foundry" ? "Answer supplied by your Foundry agent." : "Evidence supplied by the diagnostic MCP server."}`;
  } catch (error) {
    showError(error.message || "Unable to reach the local gateway.");
    $("status").textContent = "Request failed. No generated answer was substituted.";
    $("result-count").textContent = "ERROR";
  } finally {
    busy = false;
    $("submit").disabled = false;
    $("mode").disabled = false;
    $("new-conversation").disabled = false;
    document.querySelector(".results").setAttribute("aria-busy", "false");
  }
});

fetch("/api/config").then(async (response) => {
  if (!response.ok) throw new Error("Cannot read gateway configuration.");
  const config = await response.json();
  backend = config.qa_backend || "legacy";
  $("top-k").max = config.top_k_max;
  $("top-k").value = config.top_k_default;
  $("connection").textContent = backend === "foundry" ? "Foundry agent · Foundry IQ" : `${config.transport} · ${config.tool}`;
  $("mode").querySelector('[value="chat"]').disabled = !config.chat_enabled;
  $("chat-notice").textContent = config.chat_enabled
    ? "Configured. The first question verifies live readiness; source links keep their original permissions."
    : config.chat_notice;
  updateMode();
}).catch((error) => {
  $("connection").textContent = "Gateway unavailable";
  showError(error.message);
});
