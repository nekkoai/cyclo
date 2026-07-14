import { appendFile, open } from "node:fs/promises";
import { createInterface } from "node:readline";

export const MAX_USAGE_CAPTURE_BYTES = 1024 * 1024;

const INPUT_KEYS = new Set([
  "input_tokens",
  "prompt_tokens",
  "promptTokenCount",
  "inputTokenCount",
]);
const OUTPUT_KEYS = new Set([
  "output_tokens",
  "completion_tokens",
  "candidatesTokenCount",
  "outputTokenCount",
]);
const CACHE_READ_KEYS = new Set([
  "cache_read_input_tokens",
  "cached_tokens",
  "cachedContentTokenCount",
  "cacheReadInputTokens",
]);
const CACHE_WRITE_KEYS = new Set([
  "cache_creation_input_tokens",
  "cacheWriteInputTokens",
]);

function emptyUsage() {
  return {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
  };
}
function mergeUsage(target, source) {
  for (const key of Object.keys(target)) {
    target[key] = Math.max(target[key], Number(source?.[key]) || 0);
  }
  return target;
}

function walkUsage(value, result, depth = 0) {
  if (!value || typeof value !== "object" || depth > 8) return false;
  let found = false;
  if (Array.isArray(value)) {
    for (const item of value) found = walkUsage(item, result, depth + 1) || found;
    return found;
  }
  for (const [key, child] of Object.entries(value)) {
    if (typeof child === "number" && Number.isFinite(child) && child >= 0) {
      if (INPUT_KEYS.has(key)) {
        result.input_tokens = Math.max(result.input_tokens, child);
        found = true;
      } else if (OUTPUT_KEYS.has(key)) {
        result.output_tokens = Math.max(result.output_tokens, child);
        found = true;
      } else if (CACHE_READ_KEYS.has(key)) {
        result.cache_read_tokens = Math.max(result.cache_read_tokens, child);
        found = true;
      } else if (CACHE_WRITE_KEYS.has(key)) {
        result.cache_write_tokens = Math.max(result.cache_write_tokens, child);
        found = true;
      }
    }
    if (child && typeof child === "object") {
      found = walkUsage(child, result, depth + 1) || found;
    }
  }
  return found;
}

export function usageFromObject(value) {
  const result = emptyUsage();
  walkUsage(value, result);
  return result;
}

function usageDetailsFromObject(value) {
  const usage = emptyUsage();
  return { usage, found: walkUsage(value, usage) };
}

export function usageFromCapture(buffer, contentType = "") {
  const result = emptyUsage();
  const text = Buffer.isBuffer(buffer) ? buffer.toString("utf8") : String(buffer ?? "");
  if (!text) return { ...result, usage_source: "unavailable", capture_truncated: false };

  const looksSse = String(contentType).includes("text/event-stream") || /^data:/m.test(text);
  if (looksSse) {
    let foundUsage = false;
    for (const line of text.split(/\r?\n/)) {
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      try {
        const details = usageDetailsFromObject(JSON.parse(data));
        mergeUsage(result, details.usage);
        foundUsage = details.found || foundUsage;
      } catch {
        // A bounded capture may end mid-event; earlier complete events still count.
      }
    }
    return {
      ...result,
      usage_source: foundUsage ? "provider" : "unavailable",
      capture_truncated: false,
    };
  }

  try {
    const details = usageDetailsFromObject(JSON.parse(text));
    mergeUsage(result, details.usage);
    return {
      ...result,
      usage_source: details.found ? "provider" : "unavailable",
      capture_truncated: false,
    };
  } catch {
    // Best effort only: never hold up or fail a provider response for accounting.
  }
  return { ...result, usage_source: "unavailable", capture_truncated: false };
}

export function modelFromRequest(body) {
  if (!body || body.length > MAX_USAGE_CAPTURE_BYTES) return "unknown";
  try {
    const parsed = JSON.parse(Buffer.from(body).toString("utf8"));
    return typeof parsed?.model === "string" && parsed.model ? parsed.model : "unknown";
  } catch {
    return "unknown";
  }
}

export function appendAuditRecord(path, record) {
  const line = JSON.stringify(record) + "\n";
  return appendFile(path, line, { encoding: "utf8", mode: 0o600 });
}

function counters() {
  return {
    requests: 0,
    request_bytes: 0,
    response_bytes: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    statuses: {},
  };
}

function addRecord(target, record) {
  target.requests += 1;
  for (const key of [
    "request_bytes",
    "response_bytes",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
  ]) {
    target[key] += Number(record[key]) || 0;
  }
  const status = String(record.status ?? "unknown");
  target.statuses[status] = (target.statuses[status] ?? 0) + 1;
}

function addGroup(groups, name, record) {
  const key = typeof name === "string" && name ? name : "unknown";
  if (!groups.has(key)) groups.set(key, counters());
  addRecord(groups.get(key), record);
}

export function aggregateUsageRecords(records) {
  const accumulator = usageAccumulator();
  for (const record of records) addUsageRecord(accumulator, record);
  return finishUsageAccumulator(accumulator);
}

function usageAccumulator() {
  return {
    totals: counters(),
    clients: new Map(),
    teams: new Map(),
    generations: new Map(),
    providers: new Map(),
    models: new Map(),
  };
}

function addUsageRecord(accumulator, record) {
  if (!record || typeof record !== "object") return;
  addRecord(accumulator.totals, record);
  addGroup(accumulator.clients, record.client_id, record);
  addGroup(accumulator.teams, record.team_id, record);
  addGroup(accumulator.generations, record.binding_generation, record);
  addGroup(accumulator.providers, record.provider, record);
  addGroup(accumulator.models, record.model, record);
}

function finishUsageAccumulator(accumulator) {
  const { totals, clients, teams, generations, providers, models } = accumulator;
  return {
    version: 1,
    totals,
    by_client: Object.fromEntries(clients),
    by_team: Object.fromEntries(teams),
    by_generation: Object.fromEntries(generations),
    by_provider: Object.fromEntries(providers),
    by_model: Object.fromEntries(models),
  };
}

export async function aggregateUsageFile(path) {
  let file;
  try {
    file = await open(path, "r");
  } catch (exc) {
    if (exc?.code === "ENOENT") return aggregateUsageRecords([]);
    throw exc;
  }

  const accumulator = usageAccumulator();
  const input = file.createReadStream({ encoding: "utf8", autoClose: false });
  const lines = createInterface({ input, crlfDelay: Infinity });
  try {
    for await (const line of lines) {
      if (!line.trim()) continue;
      try {
        addUsageRecord(accumulator, JSON.parse(line));
      } catch {
        // Ignore one torn/corrupt line while retaining all other audit records.
      }
    }
  } finally {
    lines.close();
    input.destroy();
    await file.close();
  }
  return finishUsageAccumulator(accumulator);
}
