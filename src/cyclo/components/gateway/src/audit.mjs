import {
  closeSync,
  constants as fsConstants,
  fchmodSync,
  fstatSync,
  ftruncateSync,
  mkdirSync,
  openSync,
  readSync,
} from "node:fs";
import { appendFile, mkdir, open } from "node:fs/promises";
import { dirname } from "node:path";

import { splitPublicModelId } from "@cyclo/provider/protocol";

export const MAX_USAGE_RECORD_BYTES = 16 * 1024;

const USAGE_RECORD_FIELDS = Object.freeze([
  "timestamp",
  "model",
  "outcome",
  "latency_ms",
  "input_tokens",
  "output_tokens",
  "cached_input_tokens",
  "reasoning_tokens",
]);
const SAFE_TEXT = /^[^\u0000-\u001f\u007f]+$/u;

export function createUsageAudit(path) {
  if (typeof path !== "string" || !path) throw new TypeError("audit path is required");
  prepareAuditPath(path);
  let tail = Promise.resolve();
  let failure = null;

  async function record(value) {
    const write = tail.then(async () => {
      // An append error may have written only part of a record. Do not append
      // anything else to that uncertain tail in this process; restart repairs
      // it before accepting another inference request.
      if (failure) throw failure;
      await mkdir(dirname(path), { recursive: true, mode: 0o700 });
      await appendFile(path, JSON.stringify(value) + "\n", { encoding: "utf8", mode: 0o600 });
    });
    tail = write.catch((error) => {
      if (!failure) failure = error;
    });
    await write;
  }

  return Object.freeze({
    record,
    check() {
      if (failure) throw new Error("usage audit is unavailable", { cause: failure });
    },
  });
}

function prepareAuditPath(path) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const descriptor = openSync(
    path,
    fsConstants.O_RDWR
      | fsConstants.O_APPEND
      | fsConstants.O_CREAT
      | fsConstants.O_NOFOLLOW,
    0o600,
  );
  try {
    const information = fstatSync(descriptor);
    if (!information.isFile()) throw new Error("usage audit is not a regular file");
    repairIncompleteTail(descriptor, information.size);
    fchmodSync(descriptor, 0o600);
  } finally {
    closeSync(descriptor);
  }
}

function repairIncompleteTail(descriptor, size) {
  if (size === 0) return;
  const block = Buffer.allocUnsafe(Math.min(64 * 1024, size));
  let cursor = size;
  while (cursor > 0) {
    const length = Math.min(block.byteLength, cursor);
    cursor -= length;
    const count = readSync(descriptor, block, 0, length, cursor);
    if (count !== length) throw new Error("cannot read the usage audit tail");
    const newline = block.lastIndexOf(0x0a, count - 1);
    if (newline >= 0) {
      const completeSize = cursor + newline + 1;
      if (completeSize !== size) ftruncateSync(descriptor, completeSize);
      return;
    }
  }
  ftruncateSync(descriptor, 0);
}

export function usageRecord({ model, started, outcome, usage }) {
  return {
    timestamp: new Date().toISOString(),
    model,
    outcome,
    latency_ms: Math.max(0, Date.now() - started),
    input_tokens: safeNumber(usage?.inputTokens),
    output_tokens: safeNumber(usage?.outputTokens),
    cached_input_tokens: safeNumber(usage?.cachedInputTokens),
    reasoning_tokens: safeNumber(usage?.reasoningTokens),
  };
}

export async function aggregateUsageFile(
  path,
  { maxRecordBytes = MAX_USAGE_RECORD_BYTES } = {},
) {
  if (typeof path !== "string" || !path) throw new TypeError("usage path is required");
  if (!Number.isSafeInteger(maxRecordBytes) || maxRecordBytes <= 0) {
    throw new TypeError("maximum usage record size must be a positive integer");
  }

  let handle;
  try {
    handle = await open(path, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
  } catch (error) {
    if (error?.code === "ENOENT") return emptyUsageReport();
    throw new Error(`cannot open usage audit: ${error.message}`, { cause: error });
  }

  try {
    const information = await handle.stat();
    if (!information.isFile()) throw new Error("usage audit is not a regular file");
    if (information.size === 0) return emptyUsageReport();

    // Read a fixed snapshot. A concurrent append may be visible only in part;
    // only newline-terminated records belong to this report.
    const stream = handle.createReadStream({
      autoClose: false,
      start: 0,
      end: information.size - 1,
    });
    const report = usageAccumulator();
    let pending = Buffer.alloc(0);
    let oversized = false;
    let line = 0;

    try {
      for await (const chunk of stream) {
        let start = 0;
        while (start < chunk.byteLength) {
          const newline = chunk.indexOf(0x0a, start);
          const end = newline < 0 ? chunk.byteLength : newline;
          const fragment = chunk.subarray(start, end);
          if (!oversized) {
            const length = pending.byteLength + fragment.byteLength;
            if (length > maxRecordBytes + 1) {
              pending = Buffer.alloc(0);
              oversized = true;
            } else if (fragment.byteLength) {
              pending = pending.byteLength
                ? Buffer.concat([pending, fragment])
                : Buffer.from(fragment);
            }
          }
          if (newline < 0) break;
          line += 1;
          if (oversized) {
            throw new Error(`usage audit record ${line} exceeds ${maxRecordBytes} bytes`);
          }
          let record = pending;
          if (record.at(-1) === 0x0d) record = record.subarray(0, -1);
          addUsageLine(report, record, line, maxRecordBytes);
          pending = Buffer.alloc(0);
          oversized = false;
          start = newline + 1;
        }
      }
      // A non-newline-terminated tail may be an append currently in progress.
      // Startup will truncate it only if the writer actually crashed.
      return finishUsageReport(report);
    } finally {
      stream.destroy();
    }
  } finally {
    await handle.close();
  }
}

function emptyCounters() {
  return {
    requests: 0,
    latency_ms: 0,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cached_input_tokens: 0,
    reasoning_tokens: 0,
    outcomes: Object.create(null),
  };
}

function emptyUsageReport() {
  return {
    version: 1,
    totals: emptyCounters(),
    by_provider: {},
    by_model: {},
  };
}

function usageAccumulator() {
  return {
    totals: emptyCounters(),
    providers: new Map(),
    models: new Map(),
  };
}

function addUsageLine(report, bytes, line, maxRecordBytes) {
  if (!bytes.length) throw new Error(`usage audit record ${line} is empty`);
  if (bytes.byteLength > maxRecordBytes) {
    throw new Error(`usage audit record ${line} exceeds ${maxRecordBytes} bytes`);
  }
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch (error) {
    throw new Error(`usage audit record ${line} is not valid UTF-8`, { cause: error });
  }
  let record;
  try {
    record = JSON.parse(text);
  } catch (error) {
    throw new Error(`usage audit record ${line} is not valid JSON`, { cause: error });
  }
  const { provider, model } = validateUsageRecord(record, line);
  addCounters(report.totals, record, line);
  addGroup(report.providers, provider, record, line);
  addGroup(report.models, model, record, line);
}

function validateUsageRecord(record, line) {
  if (!record || typeof record !== "object" || Array.isArray(record)) {
    throw new Error(`usage audit record ${line} must be an object`);
  }
  const keys = Object.keys(record).sort();
  const expected = [...USAGE_RECORD_FIELDS].sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new Error(`usage audit record ${line} has an invalid schema`);
  }
  if (
    typeof record.timestamp !== "string"
    || !record.timestamp
    || !Number.isFinite(Date.parse(record.timestamp))
  ) {
    throw new Error(`usage audit record ${line} has an invalid timestamp`);
  }
  if (
    typeof record.outcome !== "string"
    || record.outcome.length > 128
    || !SAFE_TEXT.test(record.outcome)
  ) {
    throw new Error(`usage audit record ${line} has an invalid outcome`);
  }
  for (const field of USAGE_RECORD_FIELDS.slice(3)) {
    if (!Number.isSafeInteger(record[field]) || record[field] < 0) {
      throw new Error(`usage audit record ${line} has an invalid ${field}`);
    }
  }
  const route = splitPublicModelId(record.model);
  if (!route) {
    throw new Error(`usage audit record ${line} has an invalid provider/model`);
  }
  return { provider: route.provider, model: record.model };
}

function addGroup(groups, key, record, line) {
  if (!groups.has(key)) groups.set(key, emptyCounters());
  addCounters(groups.get(key), record, line);
}

function addCounters(target, record, line) {
  target.requests = checkedAdd(target.requests, 1, line);
  for (const field of [
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
  ]) {
    target[field] = checkedAdd(target[field], record[field], line);
  }
  target.total_tokens = checkedAdd(
    target.total_tokens,
    checkedAdd(record.input_tokens, record.output_tokens, line),
    line,
  );
  target.outcomes[record.outcome] = checkedAdd(
    target.outcomes[record.outcome] ?? 0,
    1,
    line,
  );
}

function checkedAdd(left, right, line) {
  const result = left + right;
  if (!Number.isSafeInteger(result)) {
    throw new Error(`usage audit counters overflow at record ${line}`);
  }
  return result;
}

function finishUsageReport(report) {
  return {
    version: 1,
    totals: report.totals,
    by_provider: sortedGroups(report.providers),
    by_model: sortedGroups(report.models),
  };
}

function sortedGroups(groups) {
  return Object.fromEntries([...groups.entries()].sort(([left], [right]) => left.localeCompare(right)));
}

function safeNumber(value) {
  if (value === undefined) return 0;
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : 0;
}
