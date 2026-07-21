import { getOAuthProviders } from "@earendil-works/pi-ai/oauth";
import {
  getBuiltinModels,
  getBuiltinProviders,
} from "@earendil-works/pi-ai/providers/all";

import { EXPOSED_APIS } from "./catalogue.mjs";

const PROVIDER_ID = /^[a-z0-9][a-z0-9_-]*$/u;
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/u;

// pi-ai owns the provider registry but does not provide safe human-readable
// descriptions for every API-key provider. Keep this table coupled to the
// pinned pi-ai release and refuse to print an unexplained future entry.
const PROVIDER_DESCRIPTIONS = Object.freeze({
  "amazon-bedrock": "AWS Bedrock model catalogue; requires AWS-specific setup",
  "ant-ling": "Ant Ling API for Ling and Ring models",
  anthropic: "Anthropic Claude API or Claude Pro/Max subscription",
  "azure-openai-responses": "Microsoft Azure OpenAI Responses API; requires an Azure endpoint",
  cerebras: "Cerebras hosted inference for fast open models",
  "cloudflare-ai-gateway": "Cloudflare AI Gateway; requires account and gateway configuration",
  "cloudflare-workers-ai": "Cloudflare Workers AI; requires Cloudflare account configuration",
  deepseek: "DeepSeek API for DeepSeek models",
  fireworks: "Fireworks AI hosted model inference",
  "github-copilot": "GitHub Copilot subscription",
  google: "Google Gemini Developer API",
  "google-vertex": "Google Vertex AI; requires Google Cloud configuration",
  groq: "Groq low-latency hosted inference",
  huggingface: "Hugging Face multi-provider inference router",
  "kimi-coding": "Kimi Coding subscription endpoint",
  minimax: "MiniMax international API",
  "minimax-cn": "MiniMax China API",
  mistral: "Mistral AI API for Mistral and Codestral models",
  moonshotai: "Moonshot AI international API for Kimi models",
  "moonshotai-cn": "Moonshot AI China API for Kimi models",
  nvidia: "NVIDIA hosted NIM inference API",
  openai: "OpenAI API for GPT and o-series models",
  "openai-codex": "OpenAI Codex through a ChatGPT Plus/Pro subscription",
  opencode: "OpenCode Zen multi-model gateway",
  "opencode-go": "OpenCode Go model gateway",
  openrouter: "OpenRouter gateway across many model providers",
  together: "Together AI hosted open-model inference",
  "vercel-ai-gateway": "Vercel AI Gateway across model providers",
  xai: "xAI API for Grok models",
  xiaomi: "Xiaomi MiMo API",
  "xiaomi-token-plan-ams": "Xiaomi MiMo token-plan endpoint for the Americas",
  "xiaomi-token-plan-cn": "Xiaomi MiMo token-plan endpoint for China",
  "xiaomi-token-plan-sgp": "Xiaomi MiMo token-plan endpoint for Singapore",
  zai: "Z.AI international coding API for GLM models",
  "zai-coding-cn": "Z.AI China coding API for GLM models",
});

export function discoverSupportedProviders({
  builtinProviders = getBuiltinProviders(),
  builtinModels = getBuiltinModels,
  oauthProviders = getOAuthProviders(),
  descriptions = PROVIDER_DESCRIPTIONS,
  exposedApis = EXPOSED_APIS,
} = {}) {
  validateProviderIds(builtinProviders, "built-in");
  if (typeof builtinModels !== "function") {
    throw new TypeError("built-in model registry must be a function");
  }
  if (!(exposedApis instanceof Set) || [...exposedApis].some(
    (api) => typeof api !== "string" || !api,
  )) {
    throw new TypeError("exposed APIs must be a set of non-empty strings");
  }
  if (
    !Array.isArray(oauthProviders)
    || oauthProviders.some(
      (provider) => !provider || typeof provider !== "object" || typeof provider.id !== "string",
    )
  ) {
    throw new Error("pi-ai returned an invalid OAuth provider registry");
  }
  const oauthIds = oauthProviders.map(({ id }) => id);
  validateProviderIds(oauthIds, "OAuth", { allowEmpty: true });
  if (!descriptions || typeof descriptions !== "object" || Array.isArray(descriptions)) {
    throw new TypeError("provider descriptions must be an object");
  }

  const oauth = new Set(oauthIds);
  const supported = builtinProviders.filter((provider) => {
    const models = builtinModels(provider);
    if (
      !Array.isArray(models)
      || models.some((model) => !model || typeof model !== "object")
    ) {
      throw new Error(`pi-ai returned invalid models for provider ${provider}`);
    }
    return models.some(({ api }) => exposedApis.has(api));
  });
  return Object.freeze(supported.sort().map((provider) => {
    const description = providerDescription(provider, descriptions);
    const supportsOAuth = oauth.has(provider);
    return Object.freeze({
      provider,
      description,
      auth: supportsOAuth ? "oauth or api-key" : "api-key",
      login: supportsOAuth
        ? `cyclo gateway login ${provider}`
        : `cyclo gateway login ${provider} --api-key-stdin`,
    });
  }));
}

export function formatSupportedProviders(providers = discoverSupportedProviders()) {
  if (!Array.isArray(providers)) throw new TypeError("providers must be an array");
  const rows = [];
  for (const provider of providers) {
    if (
      !provider
      || typeof provider !== "object"
      || !PROVIDER_ID.test(provider.provider ?? "")
      || [provider.description, provider.auth, provider.login].some(
        (value) => typeof value !== "string" || !value || CONTROL_CHARACTER.test(value),
      )
    ) {
      throw new Error("cannot format an invalid provider description");
    }
    rows.push([provider.provider, provider.description, provider.auth, provider.login]);
  }
  const widths = rows.reduce(
    (current, row) => current.map((width, index) => Math.max(width, row[index].length)),
    ["PROVIDER".length, "DESCRIPTION".length, "AUTH".length, "LOGIN COMMAND".length],
  );
  const format = (row) => row.map((value, index) => value.padEnd(widths[index])).join("  ").trimEnd();
  const lines = [
    "Supported gateway providers (login creates an account in the credential store):",
    format(["PROVIDER", "DESCRIPTION", "AUTH", "LOGIN COMMAND"]),
    format(widths.map((width) => "-".repeat(width))),
    ...rows.map(format),
    "",
    "Use --as NAME to give an account a distinct catalogue prefix. API-key logins read the key from --api-key-stdin.",
    "OAuth logins open the provider's subscription flow; no login is needed to list this table.",
  ];
  return lines.join("\n");
}

function validateProviderIds(values, label, { allowEmpty = false } = {}) {
  if (
    !Array.isArray(values)
    || (!allowEmpty && values.length === 0)
    || values.some((value) => typeof value !== "string" || !PROVIDER_ID.test(value))
    || new Set(values).size !== values.length
  ) {
    throw new Error(`pi-ai returned an invalid ${label} provider registry`);
  }
}

function providerDescription(provider, descriptions) {
  const description = Object.hasOwn(descriptions, provider)
    ? descriptions[provider]
    : undefined;
  if (
    typeof description !== "string"
    || !description.trim()
    || CONTROL_CHARACTER.test(description)
  ) {
    throw new Error(`built-in provider ${provider} has no safe description`);
  }
  return description;
}
