const CONTROL = /[\u0000-\u001f\u007f]/u;

export function createOAuthLoginCallbacks({ ask, write = console.log }) {
  if (typeof ask !== "function" || typeof write !== "function") {
    throw new TypeError("OAuth callbacks require ask and write functions");
  }
  return Object.freeze({
    onAuth(info) {
      const url = display(info?.url, "authorization URL");
      write(`\nOpen this URL to authorize the gateway:\n  ${url}`);
      if (info.instructions) write(display(info.instructions, "authorization instructions"));
      write("Paste the authorization code or full redirect URL here if the browser cannot return to this machine.");
    },
    onDeviceCode(info) {
      write(`\nOpen this URL:\n  ${display(info?.verificationUri, "device-code URL")}`);
      write(`Enter code: ${display(info?.userCode, "device code")}`);
    },
    onPrompt(prompt) {
      const message = display(prompt?.message, "prompt message");
      const placeholder = prompt?.placeholder
        ? ` (${display(prompt.placeholder, "prompt placeholder")})`
        : "";
      return ask(`${message}${placeholder} `);
    },
    onSelect(prompt) {
      return selectOAuthOption(prompt, ask, write);
    },
    onProgress(message) {
      write(display(message, "progress message"));
    },
    onManualCodeInput() {
      return ask("Paste the authorization code or full redirect URL: ");
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
