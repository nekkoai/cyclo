import { safeModelFields } from "./safe-model-fields.mjs";


const SAFE_COST_FIELDS = new Set(safeModelFields.costFields);
const SAFE_INPUT_TYPES = new Set(safeModelFields.inputTypes);
const SAFE_COMPAT_BOOLEAN_FIELDS = new Set(safeModelFields.compatBooleanFields);
const SAFE_MAX_TOKENS_FIELDS = new Set(safeModelFields.maxTokensFields);
const SAFE_THINKING_FORMATS = new Set(safeModelFields.thinkingFormats);
const SAFE_THINKING_LEVELS = new Set(safeModelFields.thinkingLevels);
const SAFE_CACHE_CONTROL_FORMATS = new Set(safeModelFields.cacheControlFormats);


export function sanitizeModel(model) {
  if (!model || typeof model !== "object" || typeof model.id !== "string" || !model.id) return null;
  const clean = { id: model.id };
  for (const key of ["name", "provider", "api"]) {
    if (typeof model[key] === "string" && model[key]) clean[key] = model[key];
  }
  if (typeof model.reasoning === "boolean") clean.reasoning = model.reasoning;
  if (Array.isArray(model.input)) {
    clean.input = model.input.filter(
      (value) => typeof value === "string" && SAFE_INPUT_TYPES.has(value),
    );
  }
  for (const key of ["contextWindow", "maxTokens"]) {
    if (Number.isSafeInteger(model[key]) && model[key] > 0) clean[key] = model[key];
  }
  if (model.cost && typeof model.cost === "object") {
    const cost = {};
    for (const key of SAFE_COST_FIELDS) {
      if (
        typeof model.cost[key] === "number"
        && Number.isFinite(model.cost[key])
        && model.cost[key] >= 0
      ) {
        cost[key] = model.cost[key];
      }
    }
    if (Object.keys(cost).length) clean.cost = cost;
  }
  if (model.compat && typeof model.compat === "object") {
    const compat = {};
    for (const key of SAFE_COMPAT_BOOLEAN_FIELDS) {
      if (typeof model.compat[key] === "boolean") compat[key] = model.compat[key];
    }
    if (SAFE_MAX_TOKENS_FIELDS.has(model.compat.maxTokensField)) {
      compat.maxTokensField = model.compat.maxTokensField;
    }
    if (SAFE_THINKING_FORMATS.has(model.compat.thinkingFormat)) {
      compat.thinkingFormat = model.compat.thinkingFormat;
    }
    if (SAFE_CACHE_CONTROL_FORMATS.has(model.compat.cacheControlFormat)) {
      compat.cacheControlFormat = model.compat.cacheControlFormat;
    }
    if (Object.keys(compat).length) clean.compat = compat;
  }
  if (model.thinkingLevelMap && typeof model.thinkingLevelMap === "object") {
    const thinkingLevelMap = {};
    for (const key of SAFE_THINKING_LEVELS) {
      const value = model.thinkingLevelMap[key];
      if (typeof value === "string" || value === null) {
        thinkingLevelMap[key] = value;
      }
    }
    if (Object.keys(thinkingLevelMap).length) clean.thinkingLevelMap = thinkingLevelMap;
  }
  return clean;
}
