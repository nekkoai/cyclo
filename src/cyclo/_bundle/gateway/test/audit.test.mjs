import assert from "node:assert/strict";
import {
  chmod,
  mkdtemp,
  readFile,
  rm,
  stat,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  aggregateUsageFile,
  createUsageAudit,
  MAX_USAGE_RECORD_BYTES,
  usageRecord,
} from "../src/audit.mjs";

test("usage records contain only observable request, outcome, and token data", () => {
  const record = usageRecord({
    model: "work/gpt-test",
    started: Date.now(),
    outcome: "ok",
    usage: {
      inputTokens: 7n,
      outputTokens: 5n,
      cachedInputTokens: 3n,
      reasoningTokens: 2n,
    },
  });

  assert.deepEqual(Object.keys(record), [
    "timestamp",
    "model",
    "outcome",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
  ]);
  assert.equal(record.model, "work/gpt-test");
  assert.equal(record.outcome, "ok");
  assert.equal(record.input_tokens, 7);
  assert.equal(record.output_tokens, 5);
  assert.equal(record.cached_input_tokens, 3);
  assert.equal(record.reasoning_tokens, 2);
  assert.ok(Number.isFinite(Date.parse(record.timestamp)));
  assert.ok(record.latency_ms >= 0);
});

test("usage records are serialized and private", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-audit-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const path = join(root, "state", "usage.jsonl");
  const audit = createUsageAudit(path);

  await Promise.all([
    audit.record({ sequence: 1 }),
    audit.record({ sequence: 2 }),
    audit.record({ sequence: 3 }),
  ]);

  assert.deepEqual(
    (await readFile(path, "utf8")).trim().split("\n").map(JSON.parse),
    [{ sequence: 1 }, { sequence: 2 }, { sequence: 3 }],
  );
  assert.equal((await stat(path)).mode & 0o777, 0o600);
  assert.doesNotThrow(() => audit.check());
});

test("an unavailable audit sink fails startup", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-audit-failure-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const blocker = join(root, "blocker");
  await writeFile(blocker, "not a directory");
  assert.throws(() => createUsageAudit(join(blocker, "usage.jsonl")));
});

test("a later audit failure is visible to health and can recover", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-audit-recovery-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const path = join(root, "usage.jsonl");
  const audit = createUsageAudit(path);

  await chmod(path, 0o400);
  await assert.rejects(audit.record({ sequence: 1 }));
  assert.throws(() => audit.check(), /unavailable/u);

  await chmod(path, 0o600);
  await audit.record({ sequence: 2 });
  assert.doesNotThrow(() => audit.check());
});

test("usage aggregation is global by provider and exact public model", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-usage-report-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const path = join(root, "usage.jsonl");
  const records = [
    usageRecord({
      model: "work/gpt-test",
      started: Date.now() - 7,
      outcome: "ok",
      usage: { inputTokens: 5n, outputTokens: 3n, cachedInputTokens: 2n },
    }),
    usageRecord({
      model: "work/gpt-test",
      started: Date.now() - 4,
      outcome: "rpc_14",
      usage: { inputTokens: 2n, outputTokens: 0n },
    }),
    usageRecord({
      model: "claude/sonnet",
      started: Date.now() - 2,
      outcome: "ok",
      usage: { inputTokens: 11n, outputTokens: 7n, reasoningTokens: 3n },
    }),
  ];
  await writeFile(path, `${records.map(JSON.stringify).join("\n")}\n`, { mode: 0o600 });

  const report = await aggregateUsageFile(path);
  assert.equal(report.version, 1);
  assert.deepEqual(Object.keys(report), ["version", "totals", "by_provider", "by_model"]);
  assert.equal(report.totals.requests, 3);
  assert.equal(report.totals.input_tokens, 18);
  assert.equal(report.totals.output_tokens, 10);
  assert.equal(report.totals.total_tokens, 28);
  assert.equal(report.totals.cached_input_tokens, 2);
  assert.equal(report.totals.reasoning_tokens, 3);
  assert.deepEqual({ ...report.totals.outcomes }, { ok: 2, rpc_14: 1 });
  assert.equal(report.by_provider.work.requests, 2);
  assert.equal(report.by_provider.work.total_tokens, 10);
  assert.equal(report.by_provider.claude.total_tokens, 18);
  assert.equal(report.by_model["work/gpt-test"].requests, 2);
  assert.equal(report.by_model["claude/sonnet"].reasoning_tokens, 3);
  assert.equal(Object.hasOwn(report, "by_client"), false);
  assert.equal(Object.hasOwn(report, "by_team"), false);
});

test("missing usage audit produces a zero global report", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-usage-missing-"));
  t.after(() => rm(root, { recursive: true, force: true }));

  const report = await aggregateUsageFile(join(root, "missing.jsonl"));
  assert.equal(report.totals.requests, 0);
  assert.equal(report.totals.total_tokens, 0);
  assert.deepEqual(report.by_provider, {});
  assert.deepEqual(report.by_model, {});
});

test("usage aggregation fails closed on malformed, oversized, and linked records", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "cyclo-gateway-usage-invalid-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const path = join(root, "usage.jsonl");

  for (const [contents, pattern] of [
    ["not-json\n", /record 1 is not valid JSON/u],
    [`${JSON.stringify({ model: "work/model" })}\n`, /invalid schema/u],
    [`${JSON.stringify({
      ...usageRecord({ model: "work/model", started: Date.now(), outcome: "ok" }),
      input_tokens: -1,
    })}\n`, /invalid input_tokens/u],
    [`${JSON.stringify(usageRecord({
      model: "model-without-provider",
      started: Date.now(),
      outcome: "ok",
    }))}\n`, /invalid provider\/model/u],
    ["\n", /record 1 is empty/u],
  ]) {
    await writeFile(path, contents, { mode: 0o600 });
    await assert.rejects(aggregateUsageFile(path), pattern);
  }

  await writeFile(path, "x".repeat(MAX_USAGE_RECORD_BYTES + 1), { mode: 0o600 });
  await assert.rejects(aggregateUsageFile(path), /exceeds 16384 bytes/u);

  const target = join(root, "target.jsonl");
  await writeFile(target, "", { mode: 0o600 });
  const linked = join(root, "linked.jsonl");
  await symlink(target, linked);
  await assert.rejects(aggregateUsageFile(linked), /cannot open usage audit/u);
});
