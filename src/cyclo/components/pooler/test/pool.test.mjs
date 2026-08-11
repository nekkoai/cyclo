import assert from "node:assert/strict";
import test from "node:test";

import { PoolScheduler } from "../src/pool.mjs";

test("requests begin in deterministic round-robin order", () => {
  const pool = new PoolScheduler(["one/model", "two/model", "three/model"]);
  assert.equal(pool.begin().next(1_000).memberModelId, "one/model");
  assert.equal(pool.begin().next(1_000).memberModelId, "two/model");
  assert.equal(pool.begin().next(1_000).memberModelId, "three/model");
  assert.equal(pool.begin().next(1_000).memberModelId, "one/model");
});

test("one request tries each member once and reports the earliest retry", () => {
  const pool = new PoolScheduler(["one/model", "two/model"]);
  const attempt = pool.begin();
  assert.equal(attempt.next(1_000).memberModelId, "one/model");
  attempt.markExhausted(new Date(5_000));
  assert.equal(attempt.next(1_000).memberModelId, "two/model");
  attempt.markExhausted(new Date(3_000));
  assert.equal(attempt.next(1_000).retryAt.getTime(), 3_000);
});

test("cooldowns expire and are shared by eligible subsets", () => {
  let now = 1_000;
  const pool = new PoolScheduler(["one", "two", "three"], { now: () => now });
  const first = pool.begin(["one", "three"]);
  assert.equal(first.next().memberModelId, "one");
  first.markExhausted(new Date(5_000));
  assert.equal(pool.begin(["one", "two"]).next().memberModelId, "two");
  assert.equal(pool.begin(["one", "three"]).next().memberModelId, "three");
  now = 5_000;
  assert.equal(pool.begin(["one", "three"]).next().memberModelId, "one");
  assert.throws(() => pool.begin(["one", "missing"]), /eligible pool members/u);
});

test("concurrent exhaustion observations retain the later safe deadline", () => {
  const pool = new PoolScheduler(["one", "two"]);
  const first = pool.begin();
  const second = pool.begin();
  const third = pool.begin();
  assert.equal(first.next(1_000).memberModelId, "one");
  assert.equal(second.next(1_000).memberModelId, "two");
  assert.equal(third.next(1_000).memberModelId, "one");
  first.markExhausted(new Date(9_000));
  third.markExhausted(new Date(5_000));
  assert.equal(pool.begin().next(6_000).memberModelId, "two");
});

test("scheduler validates configuration and attempt sequencing", () => {
  assert.throws(() => new PoolScheduler([]), /at least two/u);
  assert.throws(() => new PoolScheduler(["one", "one"]), /distinct/u);
  assert.throws(() => new PoolScheduler(["one", "two"], { now: 1 }), /clock/u);

  const attempt = new PoolScheduler(["one", "two"]).begin();
  assert.throws(() => attempt.markExhausted(new Date(1_000)), /no selected/u);
  attempt.next(1_000);
  assert.throws(() => attempt.next(1_000), /must be marked exhausted/u);
  assert.throws(
    () => attempt.markExhausted(new Date(Number.NaN)),
    /valid absolute timestamp/u,
  );
});
