# AgentWS - Generic Agent Protocol

You are an AgentWS role agent. The launcher starts you with a self-contained
prompt whose opening line is:

```text
You are agent <name> with role <role>.
```

Your agent directory is:

```text
agents/<name>/
```

## Filesystem Layout

Read `/agentws/project.cyclo` before touching project files. Cyclo creates this
read-only, instance-wide view of the project definition at launch or refresh;
it remains authoritative even when the team has its own `AGENTS.md`.

The file uses ordinary Cyclo project syntax. Its `name`, `description`, and
optional `context` explain the work. Each `mount NAME /workspace/NAME rw` is a
writable project; there may be several. Each
`mount NAME /readonly/NAME ro` is a read-only supporting input. The `team`
line names `/team`. Paths in `team` and `mount` directives are the paths
available inside this container; authored description/context text is literal.

- `/workspace` — writable project checkouts only, mounted at
  `/workspace/<name>`.
- `/readonly` — named read-only inputs such as documentation and specifications.
- `/team` — the team definition: protocol, roster, and roles; normally read-only.
- `/agentws` — AgentWS protocol and runtime state: tasks, jobs, agents, and tools.
- `/agentws/project.cyclo` — generated container-facing project definition.
- `/agentws/jobs/<job-id>/workspace` — job scratch/handoff files, not project source.

Do not mistake `/readonly`, `/team`, `/agentws`, or a job workspace for a
writable project.

All `bin/`, `tasks/`, `jobs/`, and `agents/` paths below are relative to
`$CYCLO_AGENTWS_RUNTIME` (`/agentws`). Run queue commands from that directory;
run project commands from the selected writable path below `/workspace`. If the
project definition declares no writable project, put generated artifacts in
the job workspace and report their paths to the planner.

The launcher has already selected your role, claimed one job for that role, and
recorded that job in your agent directory. Discover your assignment from files,
not from hidden state.

## Protocol Authority

This file defines the generic behavior for every AgentWS agent. Role files under
`roles/` define only role-specific responsibilities. If a role file omits a
generic rule from this file, the rule still applies.

## Tool Boundary

AgentWS tools under `bin/` are the interface to local task and job state stored
under `tasks/` and `jobs/`.

Do not bypass the AgentWS tools, edit queue machinery by hand, or debug/repair
the AgentWS task or job machinery while doing a normal project job. If a tool
fails, record the exact command and output, comment on the task, then fail or
release the current job according to the problem-handling rules. `job-fail`
publishes planner visibility itself. Before an explicit release that requires
intervention, create a distinct ordinary planner job describing that work.

## Startup

Read these files first:

```text
/agentws/project.cyclo
agents/<name>/role
agents/<name>/current-job
jobs/<job-id>/task-id
jobs/<job-id>/role
bin/task-show <task-id>
tasks/<task-id>/spec.md
tasks/<task-id>/log.md
roles/<role>.md
jobs/<job-id>/spec.md
jobs/<job-id>/log.md
```

Where:

- `<name>` is your agent name from the launch prompt.
- `<role>` is the contents of `agents/<name>/role` and must equal
  `jobs/<job-id>/role`.
- `<job-id>` is the contents of `agents/<name>/current-job`.
- `<task-id>` is the contents of `jobs/<job-id>/task-id`.

`tasks/<task-id>/` is the local execution cache. The public task interface is
the `bin/task-*` commands. Use `bin/task-show <task-id>` for the current task
view, and use task commands for comments, state, and final result.

You must read `bin/task-show <task-id>` before doing job work and use the task
as the shared context for every decision. If you cannot read the task, do not
continue with implementation, review, or integration work; fail the job with
the concrete reason so its enforced planner notification can route recovery.

Process that job only. Do not claim another job yourself. Do not wait for more
jobs. Do not invent a role. Read the assigned role file, process the
assigned job, create required normal follow-up jobs, comment on the task, mark
the assigned job done, failed, or released, and exit. Terminal success and
failure notify the planner automatically.

Before doing the job work, start the claimed job:

```sh
bin/job-start <job-id> --agent-id <name>
```

If the job is already `running` and `jobs/<job-id>/agent-id` is your agent name,
continue the job instead of starting it again.

Use helper scripts in `bin/` for queue state. Do not edit `status`, `agent-id`,
or lock files directly.

## Task Context Contract

The task is the shared history and current state for the work. The job is only
the current role-scoped unit of execution.

Every agent must:

- read `bin/task-show <task-id>` before doing job work
- use the task context to understand where the overall work stands
- keep its own work scoped to the assigned job spec and role
- keep the current task ID attached to every follow-up job
- write a task comment before closing, failing, or releasing its job
- preserve planner visibility: terminal success/failure does this
  automatically; notify explicitly before a release that needs intervention

Only an agent with role `planner` may create new tasks. Agents with other roles
must not run `task-create`. If such an agent discovers work that should become a
separate task, it creates a planner job on the current task and explains the
proposed new task.

When an agent with role `planner` creates a new task, it must create jobs linked
to that task with `bin/job-create ... --task-id <new-task-id> ...`. No job may
be created without a task ID.

Reading the task does not authorize scope expansion. If the task contains other
open concerns, use them as context, but do only the assigned job. If broader
coordination is needed, notify planner on the current task.

## Agent Directory

Your durable agent state lives under `agents/<name>/`:

```text
agents/<name>/
  name
  role
  current-job
  engine
  model
  created_at
  last_started_at
  prompt.md
  transcript.log   assistant output or rendered event transcript
  error.log        CLI stderr, warnings, and launch errors
  last-message.md  final Codex message side channel, when using Codex
```

`transcript.log` is appended by the launcher on each run. With `tools/agent`, it
contains a readable rendering of the CLI's structured event stream, including
assistant text, thinking/progress events when exposed by the CLI, and tool
activity. CLI diagnostics are kept in `error.log`. Put durable notes, scratch
files, and useful outputs in your agent directory when they should survive the
current process.

## Job Layout

Each job is a directory under `jobs/`:

```text
jobs/<job-id>/
  spec.md          complete job instructions
  task-id          task this job belongs to
  role             role assigned to this job
  status           pending, claimed, running, done, or failed
  agent-id         named agent that owns the claimed job
  log.md           append-only work log
  workspace/       scratch area for this job
  lock/            atomic claim lock
```

## Task Layout

Each task is a long-lived objective under `tasks/`:

```text
tasks/<task-id>/
  spec.md          original task objective
  state            open or done
  log.md           local task history/cache
  result.md        final task result cache, when present
```

A task is composed of jobs. A job can finish without completing the task. The
task is complete only when an agent with role `planner` decides the overall
task is complete and records the result with:

```sh
bin/task-result <task-id> <result-file>
```

`task-result` records the result and marks the task state `done`.

Use these public commands for task operations:

```sh
bin/task-show <task-id>
bin/task-comment <task-id> <message>
bin/task-state <task-id> open
bin/task-state <task-id> done -m "completed"
bin/task-result <task-id> <result-file>
bin/task-list
```

Do not mutate task state directly. Use `bin/task-*` commands; this template
stores tasks in local folders.

The only task states are:

- `open`: the task is active.
- `done`: planner has recorded the final task result.

## Statuses

Valid statuses are:

- `pending`: available to be claimed by a launcher
- `claimed`: reserved by a named agent
- `running`: actively being processed by a named agent
- `done`: finished successfully
- `failed`: cannot be completed by this workflow

The normal lifecycle is:

```text
pending -> claimed -> running -> done
                         |
                         v
                       failed
```

`job-release` moves `claimed` or `running` back to `pending` for temporary
blockers.

## Ownership

`jobs/<job-id>/agent-id` is the ownership record. Transition helpers compare the
explicit `--agent-id <name>` argument with that file. This prevents one named
agent from starting, completing, failing, or releasing a job owned by another
named agent.

## Logging

Append useful work notes to `jobs/<job-id>/log.md` as you go. Use this shape:

```markdown
## <ISO-8601 timestamp> - <short summary>

<what was done, decisions made, files changed, commands run, and results>
```

The transition helpers also append short entries for start, done, fail, release,
and reaping events.

## Creating Follow-Up Jobs

Create jobs atomically with a complete spec file. Write the spec somewhere
temporary first, then pass it to `job-create`:

```sh
cat > /tmp/<new-job-id>-spec.md <<'EOF'
# <title>

## Task
<task-id>

## Objective
<complete objective>

## Context
<background, dependencies, artifacts, and prior jobs>

## Acceptance Criteria
<checks or evidence that prove completion>

## When Done
<exact follow-up job or completion action>
EOF

bin/job-create <new-job-id> --role <role> --task-id <task-id> /tmp/<new-job-id>-spec.md
```

Do not create empty jobs. Do not create a job and then edit its `spec.md`; that
allows another process to claim incomplete work.

Every follow-up job must carry the current task ID in both places:

- the `## Task` section of the job spec
- the `--task-id <task-id>` argument to `bin/job-create`

Unless the task spec explicitly says to create a separate task, use the current
job's task ID exactly. Only an agent with role `planner` may create a
separate task first and then create jobs linked to that new task. Do not create
context-free follow-up jobs.

## Planner Visibility Rule

No job outside role `planner` may terminate silently. `bin/job-done` and
`bin/job-fail` enforce this mechanically: before publishing the terminal status,
they create or verify one deterministic `role=planner` notification for the
same task and source job. If that publication fails, the source remains
nonterminal and owned. Do not manually create a duplicate generic terminal
notification.

`job-release` is not terminal; it returns the same job to `pending`. For a
deliberate release that needs planner intervention, create a normal planner job
with a distinct ID before releasing. Automatic engine retries do not notify the
planner until the retry budget is exhausted and the job fails. The deterministic
terminal notification is reserved for the source's final outcome.

If your role is `planner`, you are already handling planner-visible work.
Before closing the job, update the task with `bin/task-comment`, decide whether
the overall task needs more jobs, and either create those jobs or record that no
further work is needed. If the agent decides the task is complete, use
`bin/task-result <task-id> <result-file>`.

The enforced terminal notification covers:

- successful completion
- failed work
- blocked work
- invalid or contradictory specs
- no-op results
- any terminal result with no obvious next role
- any handoff that also needs coordination or human visibility

Planner visibility is required even when you also create a normal follow-up job
for another role. The task is the coordination sink for the whole system.
Create an additional planner job only when it represents distinct work, such as
a documentation request or an explicit decision needed before settlement.

## Documentation Discovery Rule

If you learn durable information that is useful beyond the current job and it is
missing, incomplete, misleading, or scattered in the target project's
documentation, create a `role=planner` documentation-request job for the same
task before closing your current job.

This applies to every role. Examples include:

- build, test, or deployment procedures
- architecture facts
- non-obvious constraints
- hardware, simulator, or environment behavior
- project conventions
- dependency or tooling discoveries
- debugging knowledge that would save future work

Do not update documentation yourself unless the job spec explicitly asks you to.
Instead, ask the planner to route documentation work through the normal change
workflow: implementer -> reviewer -> judge -> committer.

Documentation-request planner job specs should include:

```markdown
# Documentation Needed: <short discovery>

## Task
<task-id>

## Source Job
<job-id>

## Discovery
<what was learned>

## Evidence
<commands, files, outputs, observations, and context>

## Suggested Location
<docs or source comments to inspect, if known>

## When Done
Decide whether this documentation update is needed for the task. If needed,
create an implementer job to update the docs, followed by review, judgment, and
integration. If not needed, record the decision with `bin/task-comment` and
close this planner job.
```

## Completing This Job

When the job is complete, create all required normal follow-up jobs first.
Every role, including planner, must comment on the task with what it did before
the terminal transition:

```sh
bin/task-comment <task-id> "<role>/<job-id>: <outcome>; follow-up: <job IDs or none>; verification: <summary>"
```

The task comment is a breadcrumb for future agents. It must mention the current
job ID, the outcome, important files or artifacts, follow-up jobs created, any
proposed new tasks, verification run or not run, and the next expected decision
or role if known. Then run exactly one terminal transition:

```sh
bin/job-done <job-id> --agent-id <name> -m "<summary>"
bin/job-fail <job-id> --agent-id <name> -m "<reason>"
bin/job-release <job-id> --agent-id <name> -m "<temporary blocker>"
```

Use `job-done` only after required normal follow-up jobs already exist. Its
planner notification is a coordination fallback, not a substitute for the
team-specific success route.

## Problem Handling

- If work succeeds, create required follow-up jobs, comment on the task with the
  outcome, and mark this job done. The terminal command publishes the planner
  notification.
- If the spec is invalid, impossible, or contradictory, comment on the task
  with the reason and mark this job failed. The terminal command publishes the
  planner notification.
- If the blocker is temporary and the same job may be valid later, notify the
  planner explicitly when intervention is needed, comment on the task with the
  blocker, and release this job.
- If another role needs to decide what happens next, create a job for that role
  and still create the task comment; the terminal command handles planner
  visibility.

## Target Modification Isolation

If a task modifies a Git-backed target, an agent with role `planner` must
create or name a dedicated branch and worktree for the change before creating
implementation jobs. That agent records the branch, worktree, and base commit
in the task with `bin/task-comment` and includes them in all implementer,
reviewer, judge, and integration job specs. The base branch must be explicit in
every job spec; it is the branch in the original checkout that receives
approved work, such as `master` or `main`.

Implementers, reviewers, and judges use only the dedicated worktree named by the
planner. Implementers edit there; reviewers and judges inspect it without
editing. None of them commit in or merge into the original project checkout.
The local committer is the exception: after a judge records an `accept` verdict,
it uses the original base checkout only to merge the named work branch into the
named base branch and run final verification.

Use this model for Git-backed changes:

```text
base checkout / base branch / base commit
        |
        | planner creates work branch and worktree
        v
worktree on task branch
        |
        | implementer changes and verifies
        v
reviewer inspects worktree and independently runs verification
        |
        | judge decides whether the reviewed outcome is acceptable
        v
judge records accept, revise, block, or escalate
        |
        | on accept, committer merges judged work branch into base branch
        v
base checkout on base branch contains accepted work
        |
        | committer runs verification again
        v
planner notification
```

Workspace details in job specs should include:

```markdown
## Workspace
Base checkout: <path to original repository checkout>
Base branch: <exact branch to merge into in the base checkout>
Base commit: <commit used to create the worktree>
Worktree: <path to dedicated worktree>
Work branch: <task branch name>
Integration role: committer
Integration action or command: <what the committer must do or run>
```

Reviewer, judge, and committer jobs must include the verification commands and
results relevant to their decision. If a verification command cannot be run,
the role must record exactly why in the job log and task comment; the terminal
notification directs planner to those records.

## Helpers

- `bin/task-create <task-id> <spec-file>`
- `bin/task-show <task-id>`
- `bin/task-comment <task-id> <message>`
- `bin/task-state <task-id> open`
- `bin/task-state <task-id> done -m "completed"`
- `bin/task-result <task-id> <result-file>`
- `bin/task-list`
- `bin/job-create <job-id> --role <role> --task-id <task-id> <spec-file>`
- `bin/job-claim [job-id] --role <role> --agent-id <agent-id>`
- `bin/job-start <job-id> --agent-id <agent-id>`
- `bin/job-done <job-id> --agent-id <agent-id> -m <message>`
- `bin/job-release <job-id> --agent-id <agent-id> -m <message>`
- `bin/job-fail <job-id> --agent-id <agent-id> -m <message>`
- `bin/job-list [status]`
- `bin/job-mine --agent-id <agent-id>`
- `bin/job-wait --role <role>`
- `bin/job-watch <status>`
- `bin/job-orphans`
- `bin/job-reset-orphans`
- `bin/job-reap [minutes]`
