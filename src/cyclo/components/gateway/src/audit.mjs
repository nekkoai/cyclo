import {
  closeSync,
  constants as fsConstants,
  fchmodSync,
  mkdirSync,
  openSync,
} from "node:fs";
import { appendFile, mkdir, open } from "node:fs/promises";
import { dirname } from "node:path";

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
const ROUTE = /^[a-z0-9][a-z0-9_-]*$/u;
const MODEL = /^[^\s\u0000-\u001f\u007f]+$/u;
const SAFE_TEXT = /^[^\u0000-\u001f\u007f]+$/u;
const RESERVED_ROUTES = new Set(["__proto__", "constructor", "gateway", "prototype"]);

export function createUsageAudit(path) {
  if (typeof path !== "string" || !path) throw new TypeError("audit path is required");
  prepareAuditPath(path);
  let tail = Promise.resolve();
  let failure = null;

  async function record(value) {
    const write = tail.then(async () => {
      await mkdir(dirname(path), { recursive: true, mode: 0o700 });
      await appendFile(path, JSON.stringify(value) + "\n", { encoding: "utf8", mode: 0o600 });
      failure = null;
    });
    tail = write.catch((error) => {
      failure = error;
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
    fsConstants.O_WRONLY
      | fsConstants.O_APPEND
      | fsConstants.O_CREAT
      | fsConstants.O_NOFOLLOW,
    0o600,
  );
  try {
    fchmodSync(descriptor, 0o600);
  } finally {
    closeSync(descriptor);
  }
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

  const report = usageAccumulator();
  const stream = handle.createReadStream({ autoClose: false });
  let pending = Buffer.alloc(0);
  let line = 0;
  try {
    const information = await handle.stat();
    if (!information.isFile()) throw new Error("usage audit is not a regular file");

    for await (const chunk of stream) {
      const data = pending.length ? Buffer.concat([pending, chunk]) : chunk;
      let start = 0;
      for (let index = data.indexOf(0x0a, start); index >= 0; index = data.indexOf(0x0a, start)) {
        line += 1;
        let record = data.subarray(start, index);
        if (record.at(-1) === 0x0d) record = record.subarray(0, -1);
        addUsageLine(report, record, line, maxRecordBytes);
        start = index + 1;
      }
      pending = Buffer.from(data.subarray(start));
      if (pending.byteLength > maxRecordBytes) {
        throw new Error(`usage audit record ${line + 1} exceeds ${maxRecordBytes} bytes`);
      }
    }
    if (pending.length) {
      line += 1;
      addUsageLine(report, pending, line, maxRecordBytes);
    }
    return finishUsageReport(report);
  } finally {
    stream.destroy();
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
  if (typeof record.model !== "string" || !MODEL.test(record.model)) {
    throw new Error(`usage audit record ${line} has an invalid model`);
  }
  const separator = record.model.indexOf("/");
  const provider = separator > 0 ? record.model.slice(0, separator) : "";
  const model = separator > 0 ? record.model : "";
  if (
    !ROUTE.test(provider)
    || RESERVED_ROUTES.has(provider)
    || separator === record.model.length - 1
  ) {
    throw new Error(`usage audit record ${line} has an invalid provider/model`);
  }
  return { provider, model };
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
