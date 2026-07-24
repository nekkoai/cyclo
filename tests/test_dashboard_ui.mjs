import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const asset = (name) => new URL(`../src/cyclo/dashboard_static/${name}`, import.meta.url);

async function dashboardHelpers(names) {
  const javascript = await readFile(asset("app.js"), "utf8");
  const instrumented = javascript.replace(
    /\n  init\(\);\n\}\)\(\);\s*$/,
    `\n  globalThis.__dashboardTest = { ${names.join(", ")} };\n})();`,
  );
  assert.notEqual(instrumented, javascript, "dashboard test hook was not installed");
  const context = {
    document: { querySelector: () => null },
    window: {
      location: new URL("https://dashboard.example.test:4173/fleet?view=all#running"),
    },
    URL,
  };
  vm.runInNewContext(instrumented, context);
  return context.__dashboardTest;
}

test("dashboard assets are self-contained and expose accessible live regions", async () => {
  const html = await readFile(asset("index.html"), "utf8");

  assert.match(html, /<html lang="en">/);
  assert.match(html, /<main id="main-content"/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /<label class="search-field" for="search-input">/);
  assert.match(html, /<template id="instance-card-template">/);
  assert.match(html, />Workspaces</);
  assert.match(html, />Read-only mounts</);
  assert.match(html, />Health</);
  assert.match(html, />Open AgentWS</);
  assert.doesNotMatch(html, />Open workspace</);
  assert.doesNotMatch(html, /value="project-read-only"/);
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
  assert.doesNotMatch(javascript, /workspaceLink\.href = raw\./);
});

test("AgentWS links use the client-facing dashboard host and change only the port", async () => {
  const helpers = await dashboardHelpers(["agentwsUrlForCurrentHost"]);
  assert.equal(helpers.agentwsUrlForCurrentHost(32853), "https://dashboard.example.test:32853/");
  assert.equal(helpers.agentwsUrlForCurrentHost(32854), "https://dashboard.example.test:32854/");
  assert.equal(helpers.agentwsUrlForCurrentHost(0), "");
});

test("dashboard normalizes workspace and read-only mount lists independently", async () => {
  const { normalizeInstance } = await dashboardHelpers(["normalizeInstance"]);

  const malformed = normalizeInstance({
    id: "malformed",
    project: { workspaces: null, read_only_mounts: { nope: true } },
  });
  assert.equal(malformed.workspaces.length, 0);
  assert.equal(malformed.readOnlyMounts.length, 0);
  assert.equal("projectReadOnly" in malformed.mode, false);

  const configured = normalizeInstance({
    id: "configured",
    project: {
      name: "Cyclo",
      path: "/host/cyclo",
      definition: "/config/project.cyclo",
      description: "Cyclo development",
      workspaces: [
        { name: "source", path: "/host/source", container_path: "/workspace/source" },
      ],
      read_only_mounts: [
        { name: "docs", path: "/host/docs", container_path: "/readonly/docs" },
      ],
    },
  });
  assert.equal(configured.project, "Cyclo");
  assert.equal(configured.projectReference, "/config/project.cyclo");
  assert.equal(configured.projectDescription, "Cyclo development");
  assert.equal(configured.workspaces[0].containerPath, "/workspace/source");
  assert.equal(configured.readOnlyMounts[0].containerPath, "/readonly/docs");

  const legacy = normalizeInstance({
    id: "legacy",
    project: "/host/legacy-project",
  });
  assert.equal(legacy.project, "/host/legacy-project");
  assert.equal(legacy.projectReference, "/host/legacy-project");

  const unsupportedAliases = normalizeInstance({
    id: "unsupported-aliases",
    project_name: "ignored-name",
    project_file: "/ignored/project.cyclo",
    project_path: "/ignored/project",
    project_description: "ignored description",
  });
  assert.equal(unsupportedAliases.project, "—");
  assert.equal(unsupportedAliases.projectReference, "—");
  assert.equal(unsupportedAliases.projectDescription, "");
});

test("dashboard keeps lifecycle state separate from provider health", async () => {
  const { normalizeInstance, computeSummary, stateLabel } = await dashboardHelpers([
    "normalizeInstance",
    "computeSummary",
    "stateLabel",
  ]);
  const instance = normalizeInstance({
    id: "provider-down",
    state: "running",
    health: { state: "provider-down", reason: "fusion stopped" },
  });

  assert.equal(instance.state, "running");
  assert.equal(instance.displayState, "attention");
  assert.equal(instance.health.state, "provider-down");
  assert.equal(instance.health.reason, "fusion stopped");
  assert.equal(stateLabel(instance), "running");
  assert.equal(computeSummary([instance]).running, 1);
  assert.equal(computeSummary([instance]).providerIssues, 1);
  assert.equal(computeSummary([instance]).attention, 1);
});

test("dashboard treats suspended AgentWS supervisors as instance attention", async () => {
  const { normalizeInstance, computeSummary, stateLabel } = await dashboardHelpers([
    "normalizeInstance",
    "computeSummary",
    "stateLabel",
  ]);
  const instance = normalizeInstance({
    id: "suspended-team",
    state: "running",
    health: {
      state: "agents-suspended",
      reason: "1 agent suspended: planner-1",
    },
  });

  assert.equal(instance.state, "running");
  assert.equal(instance.displayState, "attention");
  assert.equal(instance.health.state, "agents-suspended");
  assert.equal(instance.health.reason, "1 agent suspended: planner-1");
  assert.equal(stateLabel(instance), "running");
  assert.equal(computeSummary([instance]).providerIssues, 0);
  assert.equal(computeSummary([instance]).attention, 1);

  const plannerFailure = normalizeInstance({
    id: "planner-failure",
    state: "running",
    health: {
      state: "agents-attention",
      reason: "1 unresolved planner failure: uart-plan",
    },
  });
  assert.equal(plannerFailure.health.state, "agents-attention");
  assert.equal(plannerFailure.displayState, "attention");
});

test("dashboard preserves paused and restarting lifecycle labels", async () => {
  const { normalizeInstance, stateLabel } = await dashboardHelpers([
    "normalizeInstance",
    "stateLabel",
  ]);
  const paused = normalizeInstance({
    id: "paused-team",
    state: "paused",
    health: { state: "inactive", reason: "" },
  });
  const restarting = normalizeInstance({
    id: "restarting-team",
    state: "restarting",
    health: { state: "inactive", reason: "" },
  });

  assert.equal(paused.state, "attention");
  assert.equal(stateLabel(paused), "paused");
  assert.equal(restarting.state, "starting");
  assert.equal(stateLabel(restarting), "restarting");
});

test("dashboard preserves unknown task counts as attention", async () => {
  const { normalizeInstance, computeSummary } = await dashboardHelpers([
    "normalizeInstance",
    "computeSummary",
  ]);
  const instance = normalizeInstance({
    id: "corrupt-task",
    state: "running",
    health: { state: "ready", reason: "" },
    counts: {
      tasks: { total: 1, open: 0, closed: 0, unknown: 1 },
    },
  });

  assert.equal(instance.tasks.unknown, 1);
  assert.equal(instance.displayState, "attention");
  assert.equal(instance.failureCount, 1);
  assert.equal(computeSummary([instance]).tasks.unknown, 1);
});

test("failure sorting does not count reported unknown queue states twice", async () => {
  const { normalizeInstance } = await dashboardHelpers(["normalizeInstance"]);
  const instance = normalizeInstance({
    id: "corrupt-queue",
    state: "running",
    health: { state: "ready", reason: "" },
    counts: {
      tasks: { total: 4, open: 0, closed: 0, unknown: 4 },
      jobs: { total: 5, failed: 2, unknown: 3 },
    },
    errors: [
      "4 tasks have an unknown or unreadable state",
      "3 jobs have an unknown or unreadable status",
    ],
  });

  assert.equal(instance.failureCount, 4);
});

test("API v3 keeps gateway usage global and ignores per-instance attribution", async () => {
  const { normalizeSnapshot } = await dashboardHelpers(["normalizeSnapshot"]);
  const snapshot = normalizeSnapshot({
    version: 3,
    generated_at: "2026-07-21T12:00:00Z",
    summary: {
      running: 1,
      provider_issues: 0,
      attention: 0,
      tokens: 134,
      requests: 4,
      errors: 0,
    },
    usage: {
      totals: {
        input_tokens: 107,
        output_tokens: 27,
        requests: 4,
      },
    },
    instances: [{
      id: "alpha",
      state: "running",
      health: { state: "ready", reason: "" },
      usage: {
        input_tokens: 999_999,
        output_tokens: 999_999,
        requests: 999_999,
      },
    }],
  });

  assert.equal(snapshot.summary.tokens, 134);
  assert.equal(snapshot.summary.requests, 4);
  assert.equal(snapshot.summary.providerIssues, 0);
  assert.equal("usage" in snapshot.instances[0], false);
});

test("dashboard styles include keyboard, motion, and responsive affordances", async () => {
  const css = await readFile(asset("styles.css"), "utf8");

  assert.match(css, /:focus-visible/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /@media \(max-width: 760px\)/);
  assert.match(css, /data-state\^="provider-"/);
  assert.match(css, /data-state\^="agents-"/);
});
