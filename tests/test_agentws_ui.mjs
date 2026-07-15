import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const asset = (name) => new URL(`../src/cyclo/_agentws/template/tools/agentws-public/${name}`, import.meta.url);
const viewer = new URL("../src/cyclo/_agentws/template/tools/agentws", import.meta.url);

test("AgentWS exposes an accessible Cyclo operations dashboard without chat", async () => {
  const html = await readFile(asset("index.html"), "utf8");

  assert.match(html, /<html lang="en">/);
  assert.match(html, /<meta name="color-scheme" content="dark">/);
  assert.match(html, /<main id="main-content"/);
  assert.match(html, /class="brand-name">cyclo</);
  assert.match(html, /class="brand-context">agentws</);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /data-view="board"/);
  assert.match(html, /data-view="pipeline"/);
  assert.match(html, /data-view="agents"/);
  assert.match(html, /data-view="map"/);
  assert.doesNotMatch(html, /data-view="chat"|view-chat|<textarea/i);
  assert.doesNotMatch(html, /(?:src|href)="https?:\/\//);
});

test("AgentWS application is observation-only and defaults to tasks", async () => {
  const javascript = await readFile(asset("app.js"), "utf8");
  const server = await readFile(viewer, "utf8");

  assert.doesNotThrow(() => new vm.Script(javascript));
  assert.match(javascript, /fetch\("\/api\/snapshot"\)/);
  assert.match(javascript, /fetch\(`\/api\/file\?/);
  assert.match(javascript, /return \["board", "pipeline", "agents", "map"\]\.includes\(view\) \? view : "board"/);
  assert.doesNotMatch(javascript, /renderChat|chatDraft|\/api\/agent-input|sendAgentInput|agentComposer/);
  assert.doesNotMatch(server, /\/api\/agent-input|handle_agent_input|write_all_nonblocking/);
});

test("AgentWS styles share Cyclo tokens and responsive accessibility", async () => {
  const css = await readFile(asset("styles.css"), "utf8");

  assert.match(css, /--bg:\s*#0b0e11/);
  assert.match(css, /--green:\s*#b8f24a/);
  assert.match(css, /--cyan:\s*#66d9d2/);
  assert.match(css, /:focus-visible/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /@media \(max-width: 760px\)/);
  assert.doesNotMatch(css, /\.chat-|\.agent-composer/);
});
