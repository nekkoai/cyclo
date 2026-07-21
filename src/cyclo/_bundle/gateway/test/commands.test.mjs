import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { usageRecord } from "../src/audit.mjs";
import { main } from "../src/main.mjs";

test("providers is a pre-login command and never needs the credential store", async () => {
  const output = capture();
  await main(["providers"], {
    env: { CYCLO_GATEWAY_AUTH_JSON: "/definitely/not/read/auth.json" },
    output,
    formatProviders: () => "PROVIDER\tDESCRIPTION\tAUTH\tLOGIN\nopenai\tOpenAI API\tapi-key\tlogin",
  });
  assert.equal(
    output.text,
    "PROVIDER\tDESCRIPTION\tAUTH\tLOGIN\nopenai\tOpenAI API\tapi-key\tlogin\n",
  );
});

test("usage prints only the global audit report", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-command-usage-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const usagePath = join(root, "usage.jsonl");
  const authPath = join(root, "auth.json");
  await writeFile(
    usagePath,
    `${JSON.stringify(usageRecord({
      model: "work/gpt-test",
      started: Date.now(),
      outcome: "ok",
      usage: { inputTokens: 3n, outputTokens: 2n },
    }))}\n`,
    { mode: 0o600 },
  );
  await writeFile(authPath, '{"work":{"key":"must-never-appear"}}\n', { mode: 0o600 });
  const output = capture();

  await main(["usage"], {
    env: {
      CYCLO_GATEWAY_USAGE_JSONL: usagePath,
      CYCLO_GATEWAY_AUTH_JSON: authPath,
    },
    output,
  });

  const report = JSON.parse(output.text);
  assert.equal(report.totals.requests, 1);
  assert.equal(report.by_provider.work.total_tokens, 5);
  assert.equal(report.by_model["work/gpt-test"].requests, 1);
  assert.doesNotMatch(output.text, /must-never-appear|auth\.json|credential/u);
  assert.equal(Object.hasOwn(report, "by_client"), false);
  assert.equal(Object.hasOwn(report, "by_team"), false);
});

test("gateway informational commands reject trailing arguments", async () => {
  await assert.rejects(main(["providers", "extra"]), /usage:/u);
  await assert.rejects(main(["usage", "extra"]), /usage:/u);
});

function capture() {
  return {
    text: "",
    write(value) {
      this.text += String(value);
    },
  };
}
