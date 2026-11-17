"use strict";
const element = id => document.getElementById(id);
let ready = false;
let maxBytes = 32 * 1024 * 1024;
let busy = false;

function message(text) { element("status").textContent = text; }
function row(name, pages, bytes, result) {
  const tr = document.createElement("tr");
  for (const value of [name, pages ?? "-", bytes ?? "-", result]) {
    const td = document.createElement("td");
    td.textContent = String(value);
    tr.append(td);
  }
  element("results").prepend(tr);
}
async function api(path) {
  const response = await fetch(path, {
    headers: {Authorization: `Bearer ${element("token").value}`},
    cache: "no-store"
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  return payload;
}
function uploadFile(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const query = new URLSearchParams({
      filename: file.name, allow_large: String(element("allow-large").checked)
    });
    xhr.open("POST", `/api/upload?${query}`);
    xhr.timeout = 230000;
    xhr.setRequestHeader("Authorization", `Bearer ${element("token").value}`);
    xhr.setRequestHeader("Content-Type", "application/pdf");
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) element("progress").value = event.loaded / event.total * 100;
    };
    xhr.onerror = () => reject(new Error("Network error. Check stored documents before retrying."));
    xhr.ontimeout = () => reject(new Error("Upload timed out. Check stored documents before retrying."));
    xhr.onload = () => {
      let payload;
      try { payload = JSON.parse(xhr.responseText); }
      catch { reject(new Error(`Invalid response (HTTP ${xhr.status}). Check stored documents.`)); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new Error(payload.detail || `HTTP ${xhr.status}`));
    };
    xhr.send(file);
  });
}
element("token").addEventListener("input", () => {
  ready = false;
  element("upload").disabled = true;
});
element("connect").addEventListener("click", async () => {
  try {
    message("Checking private DNS, identity, and Storage access...");
    const result = await api("/api/status");
    ready = result.storageReady === true;
    maxBytes = result.maxBytes;
    element("upload").disabled = !ready || busy;
    message(`Storage ready through ${result.storageAddresses.join(", ")}. Limit ${Math.floor(maxBytes / 1024 / 1024)} MiB. CU ${result.cuEnabled ? "enabled" : "disabled"}.`);
  } catch (error) {
    ready = false;
    element("upload").disabled = true;
    message(error.message);
  }
});
element("upload").addEventListener("click", async () => {
  if (!ready || busy) return;
  const files = Array.from(element("files").files);
  if (!files.length) { message("Select at least one PDF."); return; }
  busy = true;
  element("upload").disabled = true;
  element("token").disabled = true;
  element("allow-large").disabled = true;
  try {
    for (const file of files) {
      element("progress").value = 0;
      if (!file.name.toLowerCase().endsWith(".pdf") || file.size > maxBytes) {
        row(file.name, null, file.size, "Rejected: PDF required within size limit.");
        continue;
      }
      message(`Uploading ${file.name}...`);
      try {
        const result = await uploadFile(file);
        row(result.blob, result.pages, result.bytes, `Stored; SHA-256 ${result.sha256}`);
      } catch (error) { row(file.name, null, file.size, error.message); }
    }
    message("Upload pass finished. Review each result; extraction/indexing is a separate step.");
  } finally {
    busy = false;
    element("token").disabled = false;
    element("allow-large").disabled = false;
    element("upload").disabled = !ready;
  }
});
element("refresh").addEventListener("click", async () => {
  try {
    const result = await api("/api/files");
    element("results").replaceChildren();
    for (const file of result.files) row(file.blob, file.pages, file.bytes, `Stored; SHA-256 ${file.sha256 || "not recorded"}`);
    message(result.truncated ? "Showing a bounded listing; more documents exist." : "Stored documents listed.");
  } catch (error) { message(error.message); }
});
