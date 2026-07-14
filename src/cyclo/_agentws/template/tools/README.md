<!-- SPDX-License-Identifier: MIT -->

# AgentWS Tools

This directory contains local helper tools for an installed AgentWS directory.

The shell launchers require `sh` and whichever agent CLI is selected (`pi`,
`codex`, or `claude`). `tools/agentws` and `tools/agent` use Python 3 stdlib
only.

## `agentws`

`agentws` is the top-level way to run AgentWS. It starts the built-in `console`
assistant, the configured team, and the local web interface for the installed
AgentWS directory. It serves the installed root by default, so from the target
project root:

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
- `[team-file]`: team file to run. Defaults to `agentws/default.team`.

## `run_agentws`

`run_agentws` starts a named team from a team file and keeps successful agents
available for later jobs. Failed agent processes restart with exponential,
capped backoff. After five consecutive process failures the affected agent is
suspended until the Cyclo container is restarted, preventing Docker's restart
policy from turning a bad configuration into a tight crash loop.

Each claimed job receives at most three model-process attempts by default. If a
process exits while it still owns the job, the wrapper releases it only while
attempts remain; on the final attempt it marks the job failed. Operator-requested
SIGINT/SIGTERM shutdown releases the job without consuming an attempt. These
safe defaults can be adjusted with positive integer environment variables:

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
protocol while accepting live messages from the web interface:

```text
planner-1 planner pi-interactive
```

The built-in `console` assistant is different: `agentws/tools/agentws` starts it
automatically as agent `console` with role `console`. It is not listed in the
team file and has no queued job.

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

The agent name is mandatory. `agent` calls `bin/agent-new`, `bin/job-wait`, and
`bin/job-claim`; the agent itself starts and completes the job according to
`AGENTS.md`. The rendered transcript is stored only in
`agents/<agent-name>/transcript.log`; the job log points to that file.

## `agent-pi-interactive`

`agent-pi-interactive` is launched by `run_agentws` for team entries that use
`pi-interactive`, and by `agentws` itself for the built-in console assistant.
It starts `pi --mode rpc`, writes the rendered transcript to
`agents/<agent-name>/transcript.log`, and listens for web input on the agent's
local `input.fifo`.

Humans normally talk to the built-in console from the `Chat` tab in
`agentws/tools/agentws`. Return sends the message; Shift+Return inserts a
newline; `Stop` sends an interrupting steer message. Agent inspectors still
provide the lower-level transcript view with explicit `Send` and `Steer`
controls.

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

## `bin/agent-new`

`bin/agent-new <agent-id> <role>` creates a named agent directory when needed
and prints its path. If the agent already has a claimed or running job, it exits
with an error instead.
