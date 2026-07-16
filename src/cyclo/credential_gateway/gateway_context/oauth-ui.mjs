// Terminal adapters for pi-ai's OAuth callback contract. Kept independent of
// pi-ai and the credential store so selection behavior is easy to test.

const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/;

function requireDisplayText(value, label) {
  if (
    typeof value !== "string" ||
    !value.trim() ||
    CONTROL_CHARACTER.test(value)
  ) {
    throw new Error(`OAuth ${label} must be non-empty display text`);
  }
  return value;
}

function validateSelectPrompt(prompt) {
  if (!prompt || typeof prompt !== "object") {
    throw new Error("OAuth selection prompt is invalid");
  }
  requireDisplayText(prompt.message, "selection message");
  if (!Array.isArray(prompt.options) || prompt.options.length === 0) {
    throw new Error("OAuth selection prompt has no options");
  }

  const ids = new Set();
  for (const option of prompt.options) {
    if (!option || typeof option !== "object") {
      throw new Error("OAuth selection prompt has an invalid option");
    }
    const id = requireDisplayText(option.id, "selection id");
    requireDisplayText(option.label, "selection label");
    if (ids.has(id)) {
      throw new Error(`OAuth selection prompt repeats option ${id}`);
    }
    ids.add(id);
  }
}

async function selectOAuthOption(prompt, ask, write = console.log) {
  validateSelectPrompt(prompt);
  if (typeof ask !== "function" || typeof write !== "function") {
    throw new TypeError("OAuth selector requires ask and write functions");
  }

  write(`\n${prompt.message}`);
  for (let index = 0; index < prompt.options.length; index += 1) {
    write(`  ${index + 1}. ${prompt.options[index].label}`);
  }

  while (true) {
    const answer = String(
      await ask(`Enter number (1-${prompt.options.length}) [1]: `),
    ).trim();
    if (!answer) return prompt.options[0].id;
    if (["q", "quit", "cancel"].includes(answer.toLowerCase())) return undefined;

    if (/^[0-9]+$/.test(answer)) {
      const selected = prompt.options[Number(answer) - 1];
      if (selected) return selected.id;
    }
    write(`Choose a number from 1 to ${prompt.options.length}, or q to cancel.`);
  }
}

function showOAuthDeviceCode(info, write = console.log) {
  if (!info || typeof info !== "object" || typeof write !== "function") {
    throw new Error("OAuth device-code response is invalid");
  }
  const verificationUri = requireDisplayText(
    info.verificationUri,
    "device-code URL",
  );
  const userCode = requireDisplayText(info.userCode, "device code");

  write(`\nOpen this URL in your browser:\n  ${verificationUri}`);
  write(`Enter code: ${userCode}`);
  if (Number.isFinite(info.expiresInSeconds) && info.expiresInSeconds > 0) {
    write(`The code expires in ${Math.ceil(info.expiresInSeconds / 60)} minutes.`);
  }
  write("");
}

function createOAuthLoginCallbacks({ ask, write = console.log }) {
  if (typeof ask !== "function" || typeof write !== "function") {
    throw new TypeError("OAuth callbacks require ask and write functions");
  }

  return {
    onAuth: (info) => {
      if (!info || typeof info !== "object") {
        throw new Error("OAuth authorization response is invalid");
      }
      const url = requireDisplayText(info.url, "authorization URL");
      write(
        `\nOpen this URL in your browser to authorize the gateway:\n  ${url}`,
      );
      if (info.instructions) {
        write(requireDisplayText(info.instructions, "authorization instructions"));
      }
      write(
        "\nThe browser will redirect to a localhost URL when you finish. If that page\n" +
          "fails to load (e.g. you authorized on a laptop while this runs over SSH),\n" +
          "copy that whole failed URL from the address bar and paste it below.",
      );
    },
    onDeviceCode: (info) => showOAuthDeviceCode(info, write),
    onPrompt: (prompt) => {
      if (!prompt || typeof prompt !== "object") {
        throw new Error("OAuth text prompt is invalid");
      }
      const message = requireDisplayText(prompt.message, "prompt message");
      const placeholder = prompt.placeholder
        ? ` (${requireDisplayText(prompt.placeholder, "prompt placeholder")})`
        : "";
      return ask(`${message}${placeholder} `);
    },
    onSelect: (prompt) => selectOAuthOption(prompt, ask, write),
    onProgress: (message) =>
      write(requireDisplayText(message, "progress message")),
    onManualCodeInput: () =>
      ask("Paste the authorization code or the full redirect URL: "),
  };
}

export {
  createOAuthLoginCallbacks,
  selectOAuthOption,
  showOAuthDeviceCode,
};
