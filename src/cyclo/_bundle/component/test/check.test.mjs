import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const checker = fileURLToPath(new URL("../src/check.mjs", import.meta.url));
const schema = fileURLToPath(new URL("../gen/schema.json", import.meta.url));

test("checker reports success with exit status zero", () => {
  const declaration = fileURLToPath(new URL("fixtures/valid.conf", import.meta.url));
  const result = spawnSync(process.execPath, [checker, declaration, schema], {
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "");
});

test("checker reports invalid declarations with exit status one", () => {
  const directory = mkdtempSync(join(tmpdir(), "cyclo-check-"));
  const declaration = join(directory, "component.conf");
  writeFileSync(
    declaration,
    "component invalid\nprovide cyclo.component.v1.Component\nrequire input missing.v1.API\n",
  );

  try {
    const result = spawnSync(process.execPath, [checker, declaration, schema], {
      encoding: "utf8",
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /unknown required interface missing\.v1\.API/u);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("checker reports command misuse with exit status two", () => {
  const result = spawnSync(process.execPath, [checker], { encoding: "utf8" });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /^usage:/u);
});

test("checker composes contracts supplied by separate interface packages", () => {
  const directory = mkdtempSync(join(tmpdir(), "cyclo-contracts-"));
  const declaration = join(directory, "component.conf");
  const domainSchema = join(directory, "domain-schema.json");
  writeFileSync(
    declaration,
    "component composed\nprovide cyclo.component.v1.Component\nrequire input example.v1.Echo\n",
  );
  writeFileSync(
    domainSchema,
    JSON.stringify({ file: [{ package: "example.v1", service: [{ name: "Echo" }] }] }),
  );

  try {
    const result = spawnSync(
      process.execPath,
      [checker, declaration, schema, domainSchema],
      { encoding: "utf8" },
    );
    assert.equal(result.status, 0, result.stderr);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("checker rejects duplicate interface identities across schemas", () => {
  const directory = mkdtempSync(join(tmpdir(), "cyclo-duplicate-contracts-"));
  const declaration = join(directory, "component.conf");
  const firstSchema = join(directory, "first-schema.json");
  const secondSchema = join(directory, "second-schema.json");
  const duplicateSchema = JSON.stringify({
    file: [{ package: "example.v1", service: [{ name: "Echo" }] }],
  });
  writeFileSync(
    declaration,
    "component duplicate\nprovide cyclo.component.v1.Component\nrequire input example.v1.Echo\n",
  );
  writeFileSync(firstSchema, duplicateSchema);
  writeFileSync(secondSchema, duplicateSchema);

  try {
    const result = spawnSync(
      process.execPath,
      [checker, declaration, schema, firstSchema, secondSchema],
      { encoding: "utf8" },
    );
    assert.equal(result.status, 1);
    assert.match(result.stderr, /duplicate interface example\.v1\.Echo/u);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
