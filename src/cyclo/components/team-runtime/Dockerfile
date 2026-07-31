FROM node:22-trixie-slim@sha256:e6d9a389d34ff9678438af985c9913fbd1eb6ed36e80fea56644f4b4f6dd70ba AS agent-tools

WORKDIR /opt/cyclo-agent-tools/lib
COPY components/team-runtime/package.json components/team-runtime/package-lock.json ./
# pi-simplify 0.2.2 declares Pi 0.75 peers. The lock keeps those peer libraries
# alongside the security-fixed Pi 0.81 CLI; --force accepts only that locked,
# intentionally side-by-side resolution.
RUN npm ci --force --omit=dev --no-audit --no-fund \
    && npm cache clean --force

# Install the ConnectRPC client packages beside the agent tools. Keeping the
# source trees at stable absolute paths lets Pi load the adapter as a local
# package without downloading or copying code when a team starts.
WORKDIR /opt/cyclo
COPY components/protocol/component/package.json components/protocol/component/package-lock.json ./protocol/component/
COPY components/protocol/component/src ./protocol/component/src
COPY components/protocol/component/gen ./protocol/component/gen
RUN cd protocol/component \
    && npm ci --omit=dev --no-audit --no-fund \
    && npm cache clean --force

COPY components/protocol/provider/package.json components/protocol/provider/package-lock.json ./protocol/provider/
COPY components/protocol/provider/src ./protocol/provider/src
COPY components/protocol/provider/gen ./protocol/provider/gen
RUN cd protocol/provider \
    && npm ci --omit=dev --no-audit --no-fund \
    && npm cache clean --force

COPY adapters/pi/package.json adapters/pi/package-lock.json ./pi-adapter/
RUN cd pi-adapter \
    && npm ci --omit=dev --no-audit --no-fund \
    && npm cache clean --force
COPY adapters/pi/src ./pi-adapter/src

# Pi and its extension must share the pi-ai instance nested under the pinned Pi
# CLI. The top-level pi-ai is intentionally older: npm keeps it there for
# plugins whose peer range predates Pi 0.81.
RUN rm -rf /opt/cyclo/pi-adapter/node_modules/@earendil-works/pi-ai \
    && mkdir -p /opt/cyclo/pi-adapter/node_modules/@earendil-works \
    && ln -s /opt/cyclo-agent-tools/lib/node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-ai \
        /opt/cyclo/pi-adapter/node_modules/@earendil-works/pi-ai

# The pinned Python image is also Debian 13/trixie, so the official Node binary
# and native npm dependencies built above share the same libc ABI.
FROM python:3.12-slim@sha256:a39549e211a16149edf74e5fdc9ef03a6767e46cd987c5048b6659b6c9904c94

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV NPM_CONFIG_PREFIX=/opt/cyclo-agent-tools
ENV PATH="/opt/cyclo-agent-tools/lib/node_modules/.bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        fd-find \
        git \
        jq \
        openssh-client \
        procps \
        ripgrep \
        util-linux \
        xz-utils \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=agent-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=agent-tools /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=agent-tools /opt/cyclo-agent-tools /opt/cyclo-agent-tools
COPY --from=agent-tools /opt/cyclo /opt/cyclo
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ARG CYCLO_HOST_UID=1000
ARG CYCLO_HOST_GID=1000
RUN set -eu; \
    getent group cyclo >/dev/null 2>&1 \
        || groupadd --non-unique --gid "${CYCLO_HOST_GID}" cyclo; \
    getent passwd cyclo >/dev/null 2>&1 \
        || useradd --non-unique --uid "${CYCLO_HOST_UID}" --gid cyclo \
            --home-dir /home/cyclo --create-home --shell /bin/bash cyclo; \
    install -d -o "${CYCLO_HOST_UID}" -g "${CYCLO_HOST_GID}" -m 0755 \
        /home/cyclo /workspace /readonly /team /agentws; \
    install -d -o "${CYCLO_HOST_UID}" -g "${CYCLO_HOST_GID}" -m 0700 \
        /agentws/tasks /agentws/jobs /agentws/agents

# AgentWS code is immutable image content. DComp mounts only the three mutable
# queue directories below it, so there is no parent/child bind overlap.
COPY --chown=${CYCLO_HOST_UID}:${CYCLO_HOST_GID} _agentws/template/ /agentws/
COPY container_runtime.py /usr/local/bin/cyclo-team-runtime
COPY components/team-runtime/entrypoint.sh /usr/local/bin/cyclo-container-entrypoint
RUN chmod 0755 \
        /usr/local/bin/cyclo-container-entrypoint \
        /usr/local/bin/cyclo-team-runtime \
    && find /agentws -type d -exec chmod 0755 {} + \
    && chmod 0700 /agentws/tasks /agentws/jobs /agentws/agents

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/cyclo-container-entrypoint"]
CMD ["python3", "/usr/local/bin/cyclo-team-runtime", "--roster", "/team/team"]

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:4137/"]
