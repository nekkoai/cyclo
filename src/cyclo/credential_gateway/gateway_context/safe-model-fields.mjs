import { readFileSync } from "node:fs";


export const SAFE_MODEL_FIELDS_URL = new URL("./safe-model-fields.json", import.meta.url);
const SCHEMA_VERSION = 1;
const LIST_KEYS = [
  "costFields",
  "inputTypes",
  "compatBooleanFields",
  "maxTokensFields",
  "thinkingFormats",
  "thinkingLevels",
  "cacheControlFormats",
];


export function loadSafeModelFields(path = SAFE_MODEL_FIELDS_URL) {
  let document;
  try {
    document = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`cannot load safe model fields manifest: ${error.message}`, { cause: error });
  }

  if (!document || typeof document !== "object" || Array.isArray(document)) {
    throw new Error("safe model fields manifest must be a JSON object");
  }

  const expectedKeys = ["schemaVersion", ...LIST_KEYS].sort();
  const actualKeys = Object.keys(document).sort();
  if (
    actualKeys.length !== expectedKeys.length
    || actualKeys.some((key, index) => key !== expectedKeys[index])
  ) {
    throw new Error(
      `safe model fields manifest has invalid keys: ${actualKeys.join(", ")}`,
    );
  }
  if (!Number.isInteger(document.schemaVersion) || document.schemaVersion !== SCHEMA_VERSION) {
    throw new Error(`safe model fields manifest requires schemaVersion ${SCHEMA_VERSION}`);
  }

  const result = { schemaVersion: document.schemaVersion };
  for (const key of LIST_KEYS) {
    const items = document[key];
    if (!Array.isArray(items) || items.length === 0) {
      throw new Error(`safe model fields manifest field ${key} must be a non-empty array`);
    }
    if (items.some((item) => typeof item !== "string" || item.length === 0)) {
      throw new Error(
        `safe model fields manifest field ${key} must contain only non-empty strings`,
      );
    }
    if (new Set(items).size !== items.length) {
      throw new Error(`safe model fields manifest field ${key} must not contain duplicates`);
    }
    result[key] = Object.freeze([...items]);
  }
  return Object.freeze(result);
}


export const safeModelFields = loadSafeModelFields();
