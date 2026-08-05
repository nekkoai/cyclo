# Cyclo team templates

These team definitions ship inside Cyclo's source distribution and wheel. A
created team is an independent Git repository; it does not retain a path or
runtime dependency on the installed template.

List the available loops:

```sh
cyclo team templates
```

Create one and assign an initial model to every roster entry:

```sh
cyclo team init ~/teams/plan-execute-verify \
  --template plan-execute-verify \
  --model openai-codex/MODEL_ID
git -C ~/teams/plan-execute-verify add .
git -C ~/teams/plan-execute-verify commit -m "Define Cyclo team"
cyclo validate ~/teams/plan-execute-verify
```

`cyclo team init` initializes Git automatically and refuses to overwrite a non-empty
destination. Run `cyclo models` first, then replace the example model with an
available `provider/model`. After creation, edit individual roster entries if
agents should use different models or providers.

Each roster line is `NAME ROLE ENGINE PROVIDER/MODEL`. Jobs route by `ROLE`,
which also selects the corresponding `roles/ROLE.md` behavior. Several agents
may share one role, and role `planner` owns task coordination.

The templates omit `AGENTS.md` intentionally. Cyclo supplies its embedded
generic filesystem task/job protocol, while each repository contains only its
loop-specific roles. No external runtime installation or source checkout is
needed.

Every template includes the required pass-through Dockerfile:

```dockerfile
ARG CYCLO_TEAM_BASE
FROM ${CYCLO_TEAM_BASE}
```

Cyclo supplies its standard team-component image as `CYCLO_TEAM_BASE`. Edit the
Dockerfile to install tools needed by the team. See the installed Cyclo
documentation for the full team-repository contract.

## Included loops

- `plan-execute-verify`: a bounded evaluator/optimizer loop for general coding
  tasks.
- `test-driven-repair`: failure reproduction, regression test, repair,
  independent judgment, and integration.
- `adversarial-audit`: parallel read-only inspection followed by an adversarial
  challenge and evidence synthesis.

These are starting points for experiments, not universally optimal teams. The
copied repository is ordinary Git content: modify its roster and roles, commit
variants, or declare its `team` line `rw` in `project.cyclo` to experiment with
self-modification. Keep it `ro` when agents should not edit their own team.
