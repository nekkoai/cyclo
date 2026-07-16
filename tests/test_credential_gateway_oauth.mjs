import { test } from "node:test";
import assert from "node:assert/strict";

import {
  createOAuthLoginCallbacks,
  selectOAuthOption,
  showOAuthDeviceCode,
} from "../src/cyclo/credential_gateway/gateway_context/oauth-ui.mjs";


const LOGIN_METHODS = {
  message: "Select login method:",
  options: [
    { id: "browser", label: "Browser login (default)" },
    { id: "device_code", label: "Device code login (headless)" },
  ],
};


test("OAuth selector uses the first option when Enter is pressed", async () => {
  const output = [];
  const selected = await selectOAuthOption(
    LOGIN_METHODS,
    async () => "",
    (line) => output.push(line),
  );

  assert.equal(selected, "browser");
  assert.match(output.join("\n"), /1\. Browser login/);
  assert.match(output.join("\n"), /2\. Device code login/);
});


test("OAuth selector retries invalid input and supports cancellation", async () => {
  const answers = ["9", "2"];
  const output = [];
  assert.equal(
    await selectOAuthOption(
      LOGIN_METHODS,
      async () => answers.shift(),
      (line) => output.push(line),
    ),
    "device_code",
  );
  assert.match(output.join("\n"), /Choose a number from 1 to 2/);

  assert.equal(
    await selectOAuthOption(LOGIN_METHODS, async () => "q", () => {}),
    undefined,
  );
});


test("OAuth device-code callback prints the URL, code, and expiry", () => {
  const output = [];
  showOAuthDeviceCode(
    {
      verificationUri: "https://example.test/device",
      userCode: "ABCD-EFGH",
      expiresInSeconds: 900,
    },
    (line) => output.push(line),
  );

  assert.match(output.join("\n"), /https:\/\/example\.test\/device/);
  assert.match(output.join("\n"), /ABCD-EFGH/);
  assert.match(output.join("\n"), /15 minutes/);
});


test("OAuth callback factory implements Pi's complete terminal contract", async () => {
  const questions = [];
  const output = [];
  const callbacks = createOAuthLoginCallbacks({
    ask: async (question) => {
      questions.push(question);
      if (question.startsWith("Enter number")) return "2";
      if (question.startsWith("Paste the authorization")) return "manual-code";
      return "prompt-answer";
    },
    write: (line) => output.push(line),
  });

  assert.deepEqual(Object.keys(callbacks).sort(), [
    "onAuth",
    "onDeviceCode",
    "onManualCodeInput",
    "onProgress",
    "onPrompt",
    "onSelect",
  ]);

  callbacks.onAuth({
    url: "https://example.test/authorize",
    instructions: "Authorize Cyclo",
  });
  callbacks.onProgress("Waiting for authorization");
  callbacks.onDeviceCode({
    verificationUri: "https://example.test/device",
    userCode: "ABCD-EFGH",
  });
  assert.equal(
    await callbacks.onPrompt({ message: "Organization?", placeholder: "acme" }),
    "prompt-answer",
  );
  assert.equal(await callbacks.onSelect(LOGIN_METHODS), "device_code");
  assert.equal(await callbacks.onManualCodeInput(), "manual-code");

  assert.match(output.join("\n"), /https:\/\/example\.test\/authorize/);
  assert.match(output.join("\n"), /Authorize Cyclo/);
  assert.match(output.join("\n"), /Waiting for authorization/);
  assert.match(output.join("\n"), /ABCD-EFGH/);
  assert.ok(questions.includes("Organization? (acme) "));
});


test("OAuth selector rejects unsafe package metadata", async () => {
  await assert.rejects(
    selectOAuthOption(
      {
        message: "Select login method:",
        options: [{ id: "bad\nvalue", label: "Bad" }],
      },
      async () => "1",
      () => {},
    ),
    /display text/,
  );
  await assert.rejects(
    selectOAuthOption(
      {
        message: "Select login method:",
        options: [
          { id: "same", label: "One" },
          { id: "same", label: "Two" },
        ],
      },
      async () => "1",
      () => {},
    ),
    /repeats option/,
  );
});
