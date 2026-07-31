# Cyclo components

This directory is the build root for Cyclo's containerized component system.
Its layout follows runtime responsibility:

```text
protocol/
  component/      ConnectRPC health and component-declaration contract
  provider/       model catalogue and opaque inference transport contract
gateway/          credential-owning root provider component
passthrough/      example intermediate provider component
team/             per-team component implementation
  agentws/        job loop, tools, roles, and read-only viewer
  pi/             in-process Provider adapter for Pi
  runtime.py      component supervisor
```

`gateway` and `passthrough` are independently runnable components. Each has a
`component.conf`, implementation, tests, and—where runnable as a container—a
Dockerfile. The packages below `protocol/` are shared interface definitions,
not running services. Cyclo creates one DComp component from `team/` for every
configured team instance. Its Pi bridge runs in that component's process and
is not an independent DComp component.

All Dockerfiles use this directory as their build context. They copy only the
protocol and implementation sources they require; their adjacent
`Dockerfile.dockerignore` files enforce that boundary.
