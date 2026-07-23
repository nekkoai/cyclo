# `project.cyclo` format

`project.cyclo` is the normal Cyclo run definition. It gives one experiment a
name and description, selects one or more team repositories, and exposes named
writable workspaces and read-only supporting directories to every selected team.

```text
# Whole-line comments and blank lines are ignored.
name core-et-uart
description Design and verify a UART IP for OpenHW CORE-V.

team ../teams/jon-rtl ro
team ../teams/rtl-auditor ro

mount source ../sources/core-et rw
mount specifications ../references/specifications ro
```

Validate and run it with:

```sh
cyclo validate /path/to/project.cyclo
cyclo run /path/to/project.cyclo
```

Or create the same validated format from the CLI:

```sh
cyclo project init /path/to/project.cyclo --team /path/to/team ro --mount source /path/to/source rw
```

Cyclo starts one independent instance for every `team` line. In the example,
the instances are named `core-et-uart-jon-rtl` and
`core-et-uart-rtl-auditor`. Each gets its own container, queue, model
capability, private network, and `/team` mount. Every instance sees the same
named directories in two separate namespaces:

```text
/workspace/source          # ../sources/core-et, read-write project
/readonly/specifications   # ../references/specifications, read-only input
```

The `/workspace` and `/readonly` parents are inert read-only layouts. Only
declared `rw` children appear below `/workspace`; only declared `ro` children
appear below `/readonly`. Cyclo also generates
`/agentws/PROJECT.md`, containing the project name, description, logical mount
paths, and access modes without host paths. The generic agent protocol and
initial prompt require every agent to read that manifest before touching
project files, even when the team supplies its own `AGENTS.md`.

## Grammar

The format is UTF-8, line-oriented, and deliberately has no quoting or shell
expansion:

```text
name <project-name>
description <free text to end of line>
team <directory> <ro|rw>
mount <mount-name> <directory> <ro|rw>
```

The rules are:

- Exactly one `name` is required. It is 1–64 lowercase letters, numbers, dots,
  underscores, or hyphens, and begins with a letter or number.
- Exactly one nonempty `description` is required. It consumes the rest of its
  line; `#` inside a description is ordinary text.
- One or more `team` lines are required. The repository name is the final path
  component. It is 1–64 letters (either case), numbers, dots, underscores, or
  hyphens, begins with a letter or number, and cannot be `.` or `..`. Team paths
  and case-insensitive team names must be unique. Cyclo requires each selected
  directory to be a valid team Git repository before starting anything.
- One or more `mount` lines are required. A mount name follows the same
  lowercase 1–64 character grammar as the project name. Mount names and
  resolved mount paths must be unique.
- A `ro` team line mounts that team read-only; a `rw` team line permits that
  team to edit its own definition. A `rw` mount is a writable project at
  `/workspace/<name>`. A `ro` mount is a supporting input at
  `/readonly/<name>`; it is not a project workspace.
- Blank lines and lines whose first non-whitespace character is `#` are
  ignored. Inline comments are not a separate syntax.
- Relative paths resolve from the directory containing `project.cyclo`, never
  from the shell's current directory. Absolute paths are accepted. Every path
  must already name a directory.
- Path tokens are unquoted and cannot contain whitespace, `~`, comma, quotes,
  or backslash. Use an absolute path or a whitespace-free relative path; Cyclo
  does not perform shell or home-directory expansion.
- Team and mounted directory trees must not overlap each other. Cyclo also rejects
  mounts overlapping its state, trusted runtime/configuration sources, the host
  Pi directory, pseudo-filesystems, or a Docker socket.
- The definition itself must be a regular, non-symlink file no larger than
  1 MiB. Tabs and other embedded control characters are rejected.
- Unknown directives fail closed. In particular, an `mcp` line is rejected
  because this Cyclo version does not yet implement MCP attachment.

Cyclo hashes the validated semantic definition—name, description, resolved
team and mount paths, order, and access modes—and records that generation in
each instance's metadata.

## Run options

`project.cyclo` owns the team and mount authority. Consequently `--name` and
`--team-write` are rejected with this form; express those choices in the file.
`--offline`, `--host`, `--image`, `--verbose`, `--build`, and `--dry-run`
still apply to the run.

By default, a team without a Dockerfile uses Cyclo's installation-scoped common
runtime image. A team with a Dockerfile gets its own installation-scoped image,
built with the exact common image ID as `CYCLO_TEAM_BASE`. An ordinary run
reuses an image built against the current base. A missing image, or one built
against another base, fails with an instruction to rerun the same command with
`--build`. Use `--build` after changing the Dockerfile or files it consumes.

`--image` deliberately bypasses that selection with one operator-supplied image
shared by every team in the definition. Cyclo validates but never builds that
image, so `--image` and `--build` are mutually exclusive.

An explicit `--port` and `--foreground` are accepted only when the definition
contains one team, because either option is ambiguous for several instances.
`--port` is also incompatible with `--offline`, which intentionally publishes
no per-team viewer.
Use `cyclo logs -f INSTANCE` for an individual instance in a multi-team run.

Cyclo validates every team and mount before starting the first container. If a
later team fails to start, it stops and revokes the instances already started
by that invocation. Persistent queue history remains available under the Cyclo
state root. Immediately before each container is created, Cyclo rechecks the
device/inode identity of every bind source and rejects non-identical overlaps
with mounts used by any running Cyclo instance. Exact reuse of the same mounted
directory remains possible. Rollback is tied to a per-launch container identity, so
it cannot stop a concurrent replacement that reused the same instance name.

Instance IDs normally combine the project and lowercased team names. Cyclo
shortens an ID longer than 64 characters and adds a stable hash suffix. Stop
all persisted instances launched from a definition path with:

```sh
cyclo stop /path/to/project.cyclo
```

Stop uses persisted lifecycle metadata rather than reparsing the current team
list. It therefore still stops instances removed from an edited definition and
works when the file is temporarily invalid or has been deleted.
