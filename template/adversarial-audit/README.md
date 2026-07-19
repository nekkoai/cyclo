# Adversarial audit

A read-only, evidence-driven review loop:

```text
planner -> threat modeler -> parallel inspectors -> challenger
                                                    -> synthesizer -> planner
```

The challenger tries to falsify every candidate finding before synthesis. This
is related to the independent-proposal and challenge structure studied in
[multi-agent debate](https://arxiv.org/abs/2305.14325), adapted here to static
project evidence rather than free-form answer voting.

Target files are treated as hostile input. Generated reports live in Cyclo's
persistent job state, never in the project.

## Prepare

Use the template installed with Cyclo. Replace the model with an exact entry
from `cyclo models`:

```sh
cyclo init ~/teams/adversarial-audit \
  --template adversarial-audit \
  --model openai-codex/MODEL_ID
git -C ~/teams/adversarial-audit add .
git -C ~/teams/adversarial-audit commit -m "Define adversarial audit team"
cyclo validate ~/teams/adversarial-audit
```

## Run

Create `~/experiments/my-project/project.cyclo`:

```text
name my-project
description Audit a project without modifying its source.
team ../../teams/adversarial-audit ro
mount source-snapshot ../../src/my-project ro
```

```sh
cyclo validate ~/experiments/my-project/project.cyclo
cyclo run --offline ~/experiments/my-project/project.cyclo
cyclo task my-project-adversarial-audit audit-001 /tmp/audit-001.md
cyclo logs -f my-project-adversarial-audit
```

The task spec should state the audit scope, assets, attacker assumptions,
excluded operations, and required evidence. The read-only input is available at
`/readonly/source-snapshot`; this audit configuration has no writable project.
Offline mode deliberately has no
per-team host viewer; use `cyclo logs`, `cyclo path`, or `cyclo dashboard`.
This team cannot see another Cyclo team's private queue or transcripts.
