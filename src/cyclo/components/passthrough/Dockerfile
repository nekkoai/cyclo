FROM node:22-slim@sha256:53ada149d435c38b14476cb57e4a7da73c15595aba79bd6971b547ceb6d018bf

WORKDIR /opt/cyclo
COPY protocol/component/package.json protocol/component/package-lock.json ./protocol/component/
COPY protocol/component/src ./protocol/component/src
COPY protocol/component/gen ./protocol/component/gen

WORKDIR /opt/cyclo/protocol/component
RUN npm ci --omit=dev

WORKDIR /opt/cyclo
COPY protocol/provider/package.json protocol/provider/package-lock.json ./protocol/provider/
COPY protocol/provider/src ./protocol/provider/src
COPY protocol/provider/gen ./protocol/provider/gen

WORKDIR /opt/cyclo/protocol/provider
RUN npm ci --omit=dev

WORKDIR /opt/cyclo
COPY passthrough/package.json passthrough/package-lock.json ./passthrough/

WORKDIR /opt/cyclo/passthrough
RUN npm ci --omit=dev

COPY --chown=node:node passthrough/component.conf ./component.conf
COPY --chown=node:node passthrough/src ./src

ENV NODE_ENV=production

USER 1000:1000
EXPOSE 50051
STOPSIGNAL SIGTERM
ENTRYPOINT ["node", "src/main.mjs"]

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD ["node", "src/healthcheck.mjs"]
