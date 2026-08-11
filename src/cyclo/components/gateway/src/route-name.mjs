import { isProviderPrefix } from "@cyclo/provider/protocol";

export function routeName(value, label) {
  if (!isProviderPrefix(value)) {
    throw new Error(
      `${label} must start with a lowercase letter or number and use at most `
      + "64 lowercase letters, numbers, underscores, or hyphens",
    );
  }
  return value;
}
