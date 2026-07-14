import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const asset = (name) => new URL(`../src/cyclo/dashboard_static/${name}`, import.meta.url);

test("dashboard assets are self-contained and expose accessible live regions", async () => {
  const html = await readFile(asset("index.html"), "utf8");

  assert.match(html, /<html lang="en">/);
  assert.match(html, /<main id="main-content"/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /<label class="search-field" for="search-input">/);
  assert.match(html, /<template id="instance-card-template">/);
  assert.match(html, /href="\/static\/styles\.css"/);
  assert.match(html, /src="\/static\/app\.js"/);
  assert.doesNotMatch(html, /(?:src|href)="https?:\/\//);
});

test("dashboard application parses and keeps API data out of HTML sinks", async () => {
  const javascript = await readFile(asset("app.js"), "utf8");

  assert.doesNotThrow(() => new vm.Script(javascript));
  assert.match(javascript, /const API_URL = "\/api\/snapshot"/);
  assert.match(javascript, /payload\.source_errors/);
  assert.match(javascript, /jobs\.unknown/);
  assert.match(javascript, /textContent/);
  assert.doesNotMatch(javascript, /\.innerHTML\s*=/);
});

test("dashboard styles include keyboard, motion, and responsive affordances", async () => {
  const css = await readFile(asset("styles.css"), "utf8");

  assert.match(css, /:focus-visible/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /@media \(max-width: 760px\)/);
});
