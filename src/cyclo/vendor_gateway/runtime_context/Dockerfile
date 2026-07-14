FROM python:3.12-slim@sha256:a39549e211a16149edf74e5fdc9ef03a6767e46cd987c5048b6659b6c9904c94

ARG NODE_MAJOR=22
ARG PI_VERSION=0.75.5
ARG PI_WEB_ACCESS_VERSION=0.13.0
ARG PI_LENS_VERSION=3.8.68
ARG PI_SIMPLIFY_VERSION=0.2.2
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1

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
        tini \
        util-linux \
        xz-utils \
    && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g \
        "@earendil-works/pi-coding-agent@${PI_VERSION}" \
        "pi-web-access@${PI_WEB_ACCESS_VERSION}" \
        "pi-lens@${PI_LENS_VERSION}" \
        "pi-simplify@${PI_SIMPLIFY_VERSION}" \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/cyclo-container-entrypoint
RUN chmod 0755 /usr/local/bin/cyclo-container-entrypoint

WORKDIR /workspace
ENTRYPOINT ["tini", "--", "/usr/local/bin/cyclo-container-entrypoint"]
CMD ["bash"]
