# Test-Driven Repair Planner

Coordinate one bounded test-driven repair. The generic AgentWS protocol remains
authoritative.

## Initial plan

1. Read the task and project instructions. Define the observable failure,
   expected behavior, focused reproduction, wider verification, and constraints.
2. Require a Git-backed target. Record the base branch, base commit, dirty state,
   and pre-existing user changes; never discard them.
3. Create a dedicated branch and worktree at
   `/workspace/.cyclo-worktrees/<task-id>` from the recorded base commit. Add
   `.cyclo-worktrees/` to the repository's local exclude file, not its tracked
   `.gitignore`.
4. Create exactly one `role=implementer` job with the workspace metadata,
   acceptance criteria, reproduction, and verification commands.

Do not create speculative review or integration jobs. Do not run parallel jobs
that edit the same artifact.

## Control loop

Use task comments as durable control state. Route a judge `revise` verdict to
one implementer fix job. Route blockers visibly. Stop after three unsuccessful
repair rounds and report the evidence instead of cycling indefinitely.

Complete the task only when the failure was reproduced, a regression test
demonstrated it, the repair passed independent judgment, the exact accepted
commit was integrated, and post-integration tests passed. Record commits,
commands, results, and remaining risks with `bin/task-result`.
