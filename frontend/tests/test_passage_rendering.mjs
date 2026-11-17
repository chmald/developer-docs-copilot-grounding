import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const browserPath = process.env.FRONTEND_TEST_BROWSER ||
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const appUrl = process.env.FRONTEND_TEST_URL || "http://127.0.0.1:8000/";
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function connect(url) {
  const socket = new WebSocket(url);
  const pending = new Map();
  let next = 0;
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    const job = pending.get(message.id);
    if (!job) return;
    pending.delete(message.id);
    message.error ? job.reject(new Error(message.error.message)) : job.resolve(message.result);
  });
  await once(socket, "open");
  return {
    socket,
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        const id = ++next;
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
  };
}

test("browser renders passive passages and answer-first conversational Q&A", {
  skip: !fs.existsSync(browserPath) && "Set FRONTEND_TEST_BROWSER to a Chromium/Edge executable.",
  timeout: 45_000,
}, async () => {
  assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(new URL(appUrl).hostname));
  const profileRoot = path.resolve(process.env.FRONTEND_TEST_PROFILE_ROOT || os.tmpdir());
  const profile = fs.mkdtempSync(path.join(profileRoot, "grounding-passages-"));
  const probe = net.createServer();
  await new Promise(resolve => probe.listen(0, "127.0.0.1", resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const browser = spawn(browserPath, [
    "--headless=new", "--disable-gpu", "--disable-extensions", "--disable-background-networking",
    "--no-first-run", "--no-default-browser-check", `--user-data-dir=${profile}`,
    `--remote-debugging-port=${port}`, appUrl,
  ], { stdio: "ignore" });
  let cdp;
  try {
    let target;
    for (let i = 0; i < 120 && !target; i++) {
      assert.equal(browser.exitCode, null, "Browser exited before readiness.");
      try {
        const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
        target = pages.find(page => page.type === "page" && page.url.startsWith(appUrl));
      } catch (_) {}
      if (!target) await wait(250);
    }
    assert.ok(target, "Browser did not expose the local frontend.");
    cdp = await connect(target.webSocketDebuggerUrl);
    let loaded = false;
    for (let i = 0; i < 120 && !loaded; i++) {
      try {
        const response = await cdp.send("Runtime.evaluate", {
          expression: "document.readyState === 'complete' && typeof renderPassages === 'function'",
          returnByValue: true,
        });
        loaded = response.result?.value === true;
      } catch (error) {
        if (!error.message.includes("Execution context was destroyed")) throw error;
      }
      if (!loaded) await wait(250);
    }
    assert.ok(loaded, "Frontend scripts did not load.");
    const response = await cdp.send("Runtime.evaluate", {
      expression: `(async () => {
        const plain = 'Register<T> compares x < 5.';
        renderPassages(plain);
        const plainPreserved = document.getElementById('passages').textContent === plain;
        window.passageExecuted = false;
        renderPassages('<p onclick="window.passageExecuted=true">Memory &amp; capacity</p>' +
          '<table style="display:none"><tr><th>Slots</th><td colspan="2">24</td></tr></table>' +
          '<script>window.passageExecuted=true</script>' +
          '<img src="/passage-test-resource" onerror="window.passageExecuted=true">' +
          '<iframe src="/passage-test-resource"></iframe>' +
          '<style>@import url("/passage-test-resource");</style>' +
          '<svg onload="window.passageExecuted=true"><script>window.passageExecuted=true</script></svg>' +
          '<a href="javascript:window.passageExecuted=true">reference label</a>' +
          '<code>&lt;script&gt;example&lt;/script&gt;</code>');
        document.querySelector('#passages p').click();
        await new Promise(resolve => setTimeout(resolve, 300));
        const host = document.getElementById('passages');
        return {
          plainPreserved, tableCount:host.querySelectorAll('table').length,
          cell:host.querySelector('td').textContent, colspan:host.querySelector('td').colSpan,
          text:host.textContent, active:host.querySelectorAll('script,img,iframe,style,svg,a').length,
          sourceAttributes:host.querySelectorAll('[onclick],[onload],[onerror],[style],[src],[href]').length,
          executed:window.passageExecuted,
          fetched:performance.getEntriesByType('resource').some(entry=>entry.name.includes('passage-test-resource'))
        };
      })()`,
      awaitPromise: true, returnByValue: true,
    });
    assert.equal(response.exceptionDetails, undefined);
    const result = response.result.value;
    assert.equal(result.plainPreserved, true);
    assert.equal(result.tableCount, 1);
    assert.equal(result.cell, "24");
    assert.equal(result.colspan, 2);
    assert.match(result.text, /Memory & capacity/);
    assert.match(result.text, /reference label/);
    assert.match(result.text, /<script>example<\/script>/);
    assert.equal(result.active, 0);
    assert.equal(result.sourceAttributes, 0);
    assert.equal(result.executed, false);
    assert.equal(result.fetched, false);
    const conversation = await cdp.send("Runtime.evaluate", {
      expression: `(async () => {
        const answerFirst = document.getElementById('mode').value === 'chat';
        const calls = [];
        window.fetch = async (url, options) => {
          if (url !== '/api/chat') throw new Error('Unexpected request: ' + url);
          calls.push(JSON.parse(options.body));
          return {ok:true, json:async () => ({
            answer:'Answer ' + calls.length, response_id:'resp_turn' + calls.length,
            citations:[{title:'Manual',uri:'https://example.com/manual',metadata:{}}],
            notice:'Returned source.',raw:{},text:''
          })};
        };
        backend = 'foundry';
        const mode = document.getElementById('mode');
        mode.querySelector('[value="chat"]').disabled = false;
        mode.value = 'chat';
        updateMode();
        async function ask(query) {
          document.getElementById('query').value = query;
          document.getElementById('question-form').dispatchEvent(new Event('submit',{cancelable:true}));
          while (busy) await new Promise(resolve => setTimeout(resolve,10));
        }
        await ask('First question');
        const firstAnswer = document.getElementById('answer').textContent;
        await ask('What about the second one?');
        const history = document.getElementById('history').textContent;
        const hasLink = document.querySelector('#citations a')?.href === 'https://example.com/manual';
        document.getElementById('new-conversation').click();
        const cleared = document.getElementById('history-section').hidden &&
          document.getElementById('output').hidden && document.getElementById('query').value === '';
        await ask('Different topic');
        return {answerFirst, calls, firstAnswer, history, hasLink, cleared,
          topKHidden:document.getElementById('top-k-option').hidden};
      })()`,
      awaitPromise: true, returnByValue: true,
    });
    assert.equal(conversation.exceptionDetails, undefined);
    const qa = conversation.result.value;
    assert.equal(qa.answerFirst, true);
    assert.equal(qa.firstAnswer, "Answer 1");
    assert.equal(qa.calls[0].previous_response_id, undefined);
    assert.equal(qa.calls[1].previous_response_id, "resp_turn1");
    assert.equal(qa.calls[2].previous_response_id, undefined);
    assert.match(qa.history, /First question/);
    assert.match(qa.history, /Answer 1/);
    assert.equal(qa.hasLink, true);
    assert.equal(qa.cleared, true);
    assert.equal(qa.topKHidden, true);
  } finally {
    if (cdp) cdp.socket.send(JSON.stringify({ id: 999999, method: "Browser.close" }));
    if (browser.exitCode === null) await Promise.race([once(browser, "exit"), wait(5000)]);
    if (browser.exitCode === null) {
      browser.kill();
      await Promise.race([once(browser, "exit"), wait(5000)]);
    }
    if (cdp) cdp.socket.close();
    assert.equal(path.dirname(profile), profileRoot);
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
  }
});
