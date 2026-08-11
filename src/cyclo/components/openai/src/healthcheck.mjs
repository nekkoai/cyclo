#!/usr/bin/env node

import { checkComponentHealth } from "@cyclo/component/health";

checkComponentHealth()
  .then((ready) => {
    process.exitCode = ready ? 0 : 1;
  })
  .catch(() => {
    process.exitCode = 1;
  });
