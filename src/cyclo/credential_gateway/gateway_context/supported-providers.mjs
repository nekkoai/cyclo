// List the gateway's built-in provider login choices without reading its
// credential store. This entrypoint is intentionally safe to run pre-login.

import { pathToFileURL } from "node:url";

const PROVIDER_ID = /^[a-z0-9][a-z0-9_-]*$/;
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/;

// pi-ai exposes canonical IDs and model metadata, but not human descriptions
// for API-key providers. Keep these tied to the pinned pi-ai release and fail
// discovery if a future registry adds an unexplained provider.
const PROVIDER_DESCRIPTIONS = Object.freeze({
  "amazon-bedrock": "AWS Bedrock model catalog; requires AWS-specific setup",
  "ant-ling": "Ant Ling API for Ling and Ring models",
  anthropic: "Anthropic Claude through a Claude Pro/Max subscription",
  "azure-openai-responses":
    "Microsoft Azure OpenAI Responses API; requires an Azure endpoint",
  cerebras: "Cerebras hosted inference for fast open models",
  "cloudflare-ai-gateway":
    "Cloudflare AI Gateway; requires account and gateway configuration",
  "cloudflare-workers-ai":
    "Cloudflare Workers AI; requires Cloudflare account configuration",
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

function validateProviderIds(values, label, { allowEmpty = false } = {}) {
  if (
    !Array.isArray(values) ||
    (!allowEmpty && values.length === 0) ||
    values.some(
      (value) => typeof value !== "string" || !PROVIDER_ID.test(value),
    ) ||
    new Set(values).size !== values.length
  ) {
    throw new Error(`pi-ai returned an invalid ${label} provider registry`);
  }
}

function providerDescription(provider, descriptions) {
  const description = Object.hasOwn(descriptions, provider)
    ? descriptions[provider]
    : undefined;
  if (
    typeof description !== "string" ||
    !description.trim() ||
    CONTROL_CHARACTER.test(description)
  ) {
    throw new Error(`built-in provider ${provider} has no safe description`);
  }
  return description;
}

function formatSupportedProviders(
  builtinProviders,
  oauthProviderIds,
  descriptions = PROVIDER_DESCRIPTIONS,
) {
  validateProviderIds(builtinProviders, "built-in");
  validateProviderIds(oauthProviderIds, "OAuth", { allowEmpty: true });
  if (
    !descriptions ||
    typeof descriptions !== "object" ||
    Array.isArray(descriptions)
  ) {
    throw new TypeError("provider descriptions must be an object");
  }

  const oauth = new Set(oauthProviderIds);
  const lines = ["PROVIDER\tDESCRIPTION\tAUTH\tLOGIN"];
  for (const provider of [...builtinProviders].sort()) {
    const usesOAuth = oauth.has(provider);
    const auth = usesOAuth ? "oauth" : "api-key";
    const login = usesOAuth
      ? `cyclo gateway login ${provider}`
      : `cyclo gateway login ${provider} --api-key-stdin`;
    lines.push(
      `${provider}\t${providerDescription(provider, descriptions)}\t${auth}\t${login}`,
    );
  }
  lines.push(
    "",
    "Account/catalogue names default to PROVIDER. Add --as NAME to choose one; NAME uses lowercase letters, numbers, underscore, or hyphen.",
  );
  return lines.join("\n");
}

async function main() {
  // Keep dependency imports out of the pure formatter so its security and
  // output contract can be tested without installing the gateway packages.
  const [{ getOAuthProviders }, registry] = await Promise.all([
    import("@earendil-works/pi-ai/oauth"),
    import("./pi-registry.mjs"),
  ]);
  registry.checkBuiltinRegistry();

  const oauthProviders = getOAuthProviders();
  if (
    !Array.isArray(oauthProviders) ||
    oauthProviders.some(
      (provider) => !provider || typeof provider.id !== "string",
    )
  ) {
    throw new Error("pi-ai returned an invalid OAuth provider registry");
  }

  console.log(
    formatSupportedProviders(
      registry.getBuiltinProviders(),
      oauthProviders.map((provider) => provider.id),
    ),
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(`provider discovery failed: ${error.message}`);
    process.exit(1);
  });
}

export { formatSupportedProviders };
