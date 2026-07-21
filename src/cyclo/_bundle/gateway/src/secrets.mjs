const MARKER = "[REDACTED]";

export function normalizeSecrets(values) {
  const unique = new Set();
  for (const value of values ?? []) {
    if (typeof value !== "string" || value.length === 0) {
      throw new TypeError("secrets must be non-empty strings");
    }
    if (MARKER.includes(value)) {
      throw new TypeError("a secret conflicts with the redaction marker");
    }
    unique.add(value);
  }
  return Object.freeze([...unique].sort((left, right) => right.length - left.length));
}

// pi-ai has already decoded native bytes into text events. Keep the suffix
// that could begin a secret so a credential split between events is removed.
export function createTextRedactor(values) {
  const secrets = normalizeSecrets(values);
  const maxLength = Math.max(0, ...secrets.map(({ length }) => length));
  let pending = "";
  let open = true;

  function process(value, final) {
    if (maxLength === 0) return value;
    const limit = final ? value.length : Math.max(0, value.length - maxLength + 1);
    const parts = [];
    let scan = 0;
    let plainStart = 0;
    while (scan < limit) {
      const secret = secrets.find((candidate) => value.startsWith(candidate, scan));
      if (!secret) {
        scan += 1;
        continue;
      }
      if (plainStart < scan) parts.push(value.slice(plainStart, scan));
      parts.push(MARKER);
      scan += secret.length;
      plainStart = scan;
    }
    if (plainStart < scan) parts.push(value.slice(plainStart, scan));
    pending = final ? "" : value.slice(scan);
    return parts.join("");
  }

  return Object.freeze({
    push(value) {
      if (!open) throw new Error("text redactor is closed");
      if (typeof value !== "string") throw new TypeError("text delta must be a string");
      const combined = pending + value;
      return process(combined, false);
    },
    flush() {
      if (!open) throw new Error("text redactor is closed");
      open = false;
      return process(pending, true);
    },
  });
}

export function redactValue(value, values) {
  return redact(value, normalizeSecrets(values));
}

function redact(value, secrets) {
  if (typeof value === "string") return redactText(value, secrets);
  if (Array.isArray(value)) return value.map((item) => redact(item, secrets));
  if (value === null || typeof value !== "object") return value;

  const result = Object.create(null);
  for (const [key, item] of Object.entries(value)) {
    if (redactText(key, secrets) !== key) {
      throw new Error("upstream object key contains a credential");
    }
    result[key] = redact(item, secrets);
  }
  return result;
}

function redactText(value, secrets) {
  let result = value;
  for (const secret of secrets) result = result.split(secret).join(MARKER);
  return result;
}
