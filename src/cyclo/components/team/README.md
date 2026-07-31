# Team component

This directory is the complete first-party implementation of Cyclo's
per-team component. Cyclo builds one common image from this directory and may
derive a team-specific image from it. Each running project/team selection is a
separate DComp component with its own queues, Pi state, mounts, Provider link,
and lifecycle.

```text
Dockerfile             common image
entrypoint.sh          privilege drop and private Pi settings setup
runtime.py             AgentWS supervisor and bounded shutdown
agentws/               job loop, generic protocol, tools, roles, and viewer
pi/                    in-process Pi-to-Provider adapter
package*.json          pinned Pi CLI and extension dependencies
```

Everything executed or installed by the team container is owned here, except
the shared Component and Provider protocol packages in `../protocol/`.
Host-side team parsing, image and DComp-definition construction, queue
inspection, compatibility checks, templates, and administration live in
`cyclo.team`; they are not copied into this image.

The Docker build context is the parent `components/` directory. The adjacent
`Dockerfile.dockerignore` admits only this component's runtime inputs and the
two shared protocol packages.
