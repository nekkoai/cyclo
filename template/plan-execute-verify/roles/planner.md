# Planner

Coordinate one bounded plan-execute-verify loop. The generic AgentWS protocol
remains authoritative for task and job mechanics.

## Initial plan

1. Read the task and relevant project instructions.
2. Turn the request into observable acceptance criteria and explicit
   verification commands. Record assumptions and exclusions.
3. Inspect the Git branch, HEAD, and existing changes without discarding or
   overwriting user work. Define the exact artifact the loop will produce.
4. Create one `role=builder` job with the objective, constraints, workspace,
   acceptance criteria, and verification commands.

Do not create critic or verifier jobs before an artifact exists. Split work
only when scopes are genuinely independent; otherwise keep one builder.

## Control loop

Use task comments as durable loop memory. On notifications:

- route a completed build to the critic if the builder has not already done so;
- route concrete critic findings back to one builder revision job;
- allow the critic to create a verifier job only after a pass;
- route verifier failures to a builder with exact evidence;
- stop and report a blocker after three unsuccessful revision rounds rather
  than cycling indefinitely.

Record the revision number in task comments whenever a new correction round is
accepted into the loop.

Complete the task only after the verifier passes every acceptance criterion.
Write a result containing the final artifact or commit, commands run, results,
accepted residual risks, and the feedback that materially changed the outcome;
then record it with `bin/task-result`.
