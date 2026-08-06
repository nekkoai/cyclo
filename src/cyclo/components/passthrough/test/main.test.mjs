import assert from "node:assert/strict";
import test from "node:test";

import { main } from "../src/main.mjs";

test("the passthrough component accepts no command arguments", async () => {
  await assert.rejects(main(["serve"]), /usage: cyclo-passthrough-component/u);
});
