# Cyclo components

This directory is the build root for Cyclo's containerized component system.
Its layout follows runtime responsibility:

```text
protocol/
  component/      ConnectRPC health and component-declaration contract
  provider/       model catalogue and opaque inference transport contract
gateway/          credential-owning root provider component
passthrough/      example intermediate provider component
team-runtime/     common AgentWS and Pi container image
```

`gateway` and `passthrough` are independently runnable components. Each has a
`component.conf`, implementation, tests, and—where runnable as a container—a
Dockerfile. The packages below `protocol/` are shared interface definitions,
not running services. `team-runtime` is the agent workload image. The
team-side Pi bridge is an in-process package under `../adapters/pi`, not a
DComp component.

All Dockerfiles use this directory as their build context. They copy only the
protocol and implementation sources they require; their adjacent
`Dockerfile.dockerignore` files enforce that boundary.
