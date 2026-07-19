<!-- SPDX-License-Identifier: MIT -->

# AgentWS Tools

This directory contains local helper tools for an installed AgentWS directory.

The shell launchers require `sh` and whichever agent CLI is selected (`pi`,
`codex`, or `claude`). `tools/agentws` and `tools/agent` use Python 3 stdlib
only.

## `agentws`

`agentws` is the top-level way to run AgentWS. It starts the configured
processes and a local, observation-only web interface for the installed AgentWS
directory. It serves the installed root by default, so from the target project
root:

```sh
agentws/tools/agentws
```

Then open the printed local URL.
By default, AgentWS starts at `http://127.0.0.1:4137` and increases the port
until it finds a free one.

Options:

- `--root <path>`: serve a different AgentWS root.
- `--workspace <path>`: agent process working directory. Defaults to the
  AgentWS root; task/job/agent state and `bin/` tools remain rooted at `--root`.
- `--team-manifest <path>`: load a portable `agentws-team.json` bundle. The
  roster, protocol, and roles paths are resolved inside the manifest directory.
- `--host <host>`: bind host. Defaults to `127.0.0.1`.
- `--port <port>`: starting port to bind. AgentWS tries this port and then
  increasing ports until one is free. Defaults to `4137`.
- `--verbose`: print agent transcript output in this terminal.
- `--no-team`: serve the web interface without starting agents.
- `--no-console`: do not start the built-in console assistant.
- `--console-model <model>`: pass a model to the built-in console assistant.
- `--read-only`: deprecated compatibility flag; the web interface is always
  observation-only.
- `[team-file]`: team file to run. Defaults to `agentws/default.team`.

## `run_agentws`

`run_agentws` starts a named team from a team file and keeps successful agents
available for later jobs. A worker that durably releases or fails its job
restarts with exponential, capped backoff. After five consecutive settled
retry exits the affected agent is suspended, preventing a bad model or job
from becoming a tight retry loop.

Each worker is a Linux child subreaper. Before a local retry it terminates and
reaps the engine's complete adopted process tree, including descendants that
created new sessions. If that cleanup cannot be proven, the worker exits
unclean instead of starting beside a leftover engine.

Any other worker exit is unclean: `run_agentws` exits, Cyclo's PID 1 exits, and
Docker restarts the same container without rebuilding it. Docker teardown is
the process-tree fence. Before new workers start, the runtime resets persisted
`claimed` and `running` jobs to `pending` and clears stale agent assignments.
Recovery requires an inherited capability for the runtime's exclusive queue
lifetime lock, so a live agent cannot invoke the all-active reset.

Each claimed job receives at most three model-process attempts by default. If a
process exits while it still owns the job, the per-agent worker releases it
only while attempts remain. On the final automatic failure of a non-planner
job, it first creates or verifies one deterministic planner recovery job for
the same task, then marks the source job failed. Repeating settlement reuses
that job, and a planner failure never creates another planner job. This does
not change explicit agent-driven `job-fail`: the agent remains responsible for
the protocol-required follow-up work in that case.
When Cyclo supplies its provider-runtime health URL, workers wait for runtime
health before claiming. A runtime loss immediately after claim or during an
engine invocation releases the job and restores its prior attempt count; it is
not charged to the agent suspension circuit breaker.
Operator-requested SIGINT/SIGTERM shutdown releases the job without consuming
an attempt. These safe defaults can be adjusted with positive integer
environment variables:

- `AGENTWS_MAX_JOB_ATTEMPTS` (default `3`, maximum `100`)
- `AGENTWS_MAX_CONSECUTIVE_FAILURES` (default `5`, maximum `100`)
- `AGENTWS_RETRY_INITIAL_SECONDS` (default `2`)
- `AGENTWS_RETRY_MAX_SECONDS` (default `30`, maximum `3600`)

By default agents run headless. Use `--verbose` to print each agent's rendered
transcript output to the terminal, prefixed by agent name.

```sh
agentws/tools/run_agentws --verbose
agentws/tools/run_agentws
```

Team file format:

```text
# <name> <role> <agent> [model]
planner-1 planner pi
implementer-1 implementer codex
reviewer-1 reviewer claude sonnet
judge-1 judge pi
```

Role names are project-defined. Add `roles/<role>.md`, then use that role in a
team entry and in jobs. The runner and web pipeline do not require roles to be
registered in code.

Use `pi-interactive` for a Pi agent that keeps the normal AgentWS role and job
protocol while exposing Pi's RPC input FIFO to external tooling:

```text
planner-1 planner pi-interactive
```

The built-in `console` assistant is different: `agentws/tools/agentws` can start
it as agent `console` with role `console`. It is not listed in the team file and
has no queued job. Cyclo starts its AgentWS viewer with `--no-console`.

## `agent`

`agent` starts one named agent, claims one pending job for that agent's role,
records the job in `agents/<agent-name>/current-job`, and renders CLI event
output to `agents/<agent-name>/transcript.log`.
By default it also prints the rendered transcript to stdout. Use `--headless`
to write files only.

From the target project root:

```sh
# Start a Pi planner agent.
agentws/tools/agent --pi planner planner-1

# Start a named Codex implementer agent.
agentws/tools/agent --codex implementer implementer-1

# Start a Claude reviewer agent with a specific model.
agentws/tools/agent --claude -m sonnet reviewer reviewer-1
```

Options:

- `--pi`: use Pi. This is the default.
- `--codex`: use Codex CLI.
- `--claude`: use Claude Code.
- `--headless`: do not print the rendered transcript to stdout.
- `-m <model>`: pass a model name to the selected CLI.

CLI stderr is saved in `error.log`.

The agent name is mandatory. `agent` calls `bin/agent-new` and repeatedly calls
`bin/job-claim` with a bounded wait between empty-queue checks. The agent itself
starts and completes the claimed job according to
`AGENTS.md`. The rendered transcript is stored only in
`agents/<agent-name>/transcript.log`; the job log points to that file.

## `agent-pi-interactive`

`agent-pi-interactive` is launched by `run_agentws` for team entries that use
`pi-interactive`, and by `agentws` itself for the built-in console assistant.
It starts `pi --mode rpc`, writes the rendered transcript to
`agents/<agent-name>/transcript.log`, and exposes a local `input.fifo` for
external RPC tooling. The AgentWS web viewer displays transcripts and errors
but never sends or steers agent input.

## Task Commands

Task commands operate on local folders under `agentws/tasks/`:

```sh
agentws/bin/task-create <task-id> <spec-file>
agentws/bin/task-show <task-id>
agentws/bin/task-comment <task-id> <message>
agentws/bin/task-state <task-id> open
agentws/bin/task-state <task-id> done -m "completed"
agentws/bin/task-result <task-id> <result-file>
agentws/bin/task-list
```

Every task owns a persistent `.control.lock`. Creation publishes the complete
task atomically, and each result/state file replacement is atomic. Commands
that update multiple task files hold the task mutex for the full operation, so
concurrent mutations cannot interleave. The mutex serializes writers; it does
not turn a multi-file update into a crash transaction.

## `bin/agent-new`

`bin/agent-new <agent-id> <role>` creates a named agent directory when needed
and prints its path. If the agent already has a claimed or running job, it exits
with an error instead.
