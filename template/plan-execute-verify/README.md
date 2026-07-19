# Plan, execute, verify

A bounded general-purpose software loop:

```text
planner -> builder -> critic -> verifier -> planner
              ^         |
              +-- revise+
```

The planner defines observable acceptance criteria. The builder produces one
scoped artifact and records a short reflection. The critic acts as an
evaluator, sending concrete feedback back to the builder when necessary. A
separate verifier checks the accepted artifact from the task's point of view.

This combines the evaluator/optimizer pattern described in
[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
with durable verbal feedback in the spirit of
[Reflexion](https://arxiv.org/abs/2303.11366).

## Prepare

Use the template installed with Cyclo. Replace the model with an exact entry
from `cyclo models`:

```sh
cyclo init ~/teams/plan-execute-verify \
  --template plan-execute-verify \
  --model openai-codex/MODEL_ID
git -C ~/teams/plan-execute-verify add .
git -C ~/teams/plan-execute-verify commit -m "Define plan-execute-verify team"
cyclo validate ~/teams/plan-execute-verify
```

## Run

Create `~/experiments/my-project/project.cyclo`:

```text
name my-project
description Plan, implement, and independently verify changes.
team ../../teams/plan-execute-verify ro
mount source ../../src/my-project rw
```

```sh
cyclo validate ~/experiments/my-project/project.cyclo
cyclo run ~/experiments/my-project/project.cyclo
cyclo task my-project-plan-execute-verify change-001 /tmp/change-001.md
cyclo logs -f my-project-plan-execute-verify
```

The team definition remains read-only and `/workspace/source` is writable;
task paths into the checkout begin with `source/`. Add
`--offline` when the task needs only the model gateway and already-local tools;
offline mode has no per-team host viewer. The host-wide dashboard remains
available with `cyclo dashboard`.
