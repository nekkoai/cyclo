const CONTROL = /[\u0000-\u001f\u007f]/u;

export function createAuthInteraction({ ask, askSecret = ask, write = console.log, signal }) {
  if (typeof ask !== "function" || typeof askSecret !== "function" || typeof write !== "function") {
    throw new TypeError("OAuth interaction requires ask, askSecret, and write functions");
  }
  return Object.freeze({
    ...(signal === undefined ? {} : { signal }),
    async prompt(prompt) {
      if (!prompt || typeof prompt !== "object") {
        throw new Error("OAuth provider emitted an invalid prompt");
      }
      if (prompt.type === "select") {
        const selected = await selectOAuthOption(prompt, ask, write);
        if (selected === undefined) throw new Error("Login cancelled");
        return selected;
      }
      if (!["text", "secret", "manual_code"].includes(prompt.type)) {
        throw new Error(`OAuth provider emitted an unknown prompt type: ${prompt.type}`);
      }
      const message = display(prompt.message, "prompt message");
      const placeholder = prompt.placeholder
        ? ` (${display(prompt.placeholder, "prompt placeholder")})`
        : "";
      const question = `${message}${placeholder} `;
      return prompt.type === "secret" ? askSecret(question) : ask(question);
    },
    notify(event) {
      if (!event || typeof event !== "object") {
        throw new Error("OAuth provider emitted an invalid event");
      }
      if (event.type === "info") {
        write(display(event.message, "information message"));
        for (const link of event.links ?? []) {
          const label = link.label ? `${display(link.label, "link label")}: ` : "";
          write(`${label}${display(link.url, "information URL")}`);
        }
        return;
      }
      if (event.type === "auth_url") {
        write(`\nOpen this URL to authorize the gateway:\n  ${display(event.url, "authorization URL")}`);
        if (event.instructions) write(display(event.instructions, "authorization instructions"));
        return;
      }
      if (event.type === "device_code") {
        write(`\nOpen this URL:\n  ${display(event.verificationUri, "device-code URL")}`);
        write(`Enter code: ${display(event.userCode, "device code")}`);
        return;
      }
      if (event.type === "progress") {
        write(display(event.message, "progress message"));
        return;
      }
      throw new Error(`OAuth provider emitted an unknown event type: ${event.type}`);
    },
  });
}

export async function selectOAuthOption(prompt, ask, write = console.log) {
  if (!Array.isArray(prompt?.options) || prompt.options.length === 0) {
    throw new Error("OAuth selection prompt has no options");
  }
  write(`\n${display(prompt.message, "selection message")}`);
  const ids = new Set();
  for (const [index, option] of prompt.options.entries()) {
    const id = display(option?.id, "selection id");
    if (ids.has(id)) throw new Error(`OAuth selection prompt repeats option ${id}`);
    ids.add(id);
    write(`  ${index + 1}. ${display(option?.label, "selection label")}`);
  }
  while (true) {
    const answer = String(await ask(`Enter number (1-${prompt.options.length}) [1]: `)).trim();
    if (!answer) return prompt.options[0].id;
    if (["q", "quit", "cancel"].includes(answer.toLowerCase())) return undefined;
    if (/^[0-9]+$/u.test(answer)) {
      const selected = prompt.options[Number(answer) - 1];
      if (selected) return selected.id;
    }
    write(`Choose 1-${prompt.options.length}, or q to cancel.`);
  }
}

function display(value, label) {
  if (typeof value !== "string" || !value.trim() || CONTROL.test(value)) {
    throw new Error(`OAuth ${label} must be non-empty display text`);
  }
  return value;
}
