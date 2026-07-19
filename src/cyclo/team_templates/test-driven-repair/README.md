# Test-driven repair

A bounded repair loop with an explicit evidence chain and Git integration:

```text
planner -> implementer -> judge -> committer -> planner
                ^           |
                +-- revise -+
```

The implementer reproduces the failure and establishes a regression test before
fixing production code. The judge independently reviews and reruns verification.
Only an accepted change reaches the committer, and the planner closes the task
only after post-integration tests pass.

The focus on a concise agent-computer interface, exact commands, and explicit
feedback is informed by [SWE-agent](https://arxiv.org/abs/2405.15793) and modern
agent evaluation practice.

## Prepare

Use the template installed with Cyclo. Replace the model with an exact entry
from `cyclo models`:

```sh
cyclo init ~/teams/test-driven-repair \
  --template test-driven-repair \
  --model openai-codex/MODEL_ID
git -C ~/teams/test-driven-repair add .
git -C ~/teams/test-driven-repair commit -m "Define test-driven repair team"
cyclo validate ~/teams/test-driven-repair
```

## Run

The target must be a writable Git checkout with a usable test command:

Create `~/experiments/my-project/project.cyclo`:

```text
name my-project
description Reproduce, repair, judge, and integrate a project failure.
team ../../teams/test-driven-repair ro
mount source ../../src/my-project rw
```

```sh
cyclo validate ~/experiments/my-project/project.cyclo
cyclo run ~/experiments/my-project/project.cyclo
cyclo task my-project-test-driven-repair repair-001 /tmp/repair-001.md
cyclo logs -f my-project-test-driven-repair
```

A good task spec states the observed failure, expected behavior, reproduction,
focused and wider verification commands, and compatibility constraints. The
team repository stays read-only; this loop modifies the project. Use
`source/` at the start of task paths into the checkout. Use `--offline` only
when all required dependencies are already local.
