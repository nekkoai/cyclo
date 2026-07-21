FROM node:22-slim@sha256:53ada149d435c38b14476cb57e4a7da73c15595aba79bd6971b547ceb6d018bf

WORKDIR /opt/cyclo
COPY component/package.json component/package-lock.json ./component/
COPY component/src ./component/src
COPY component/gen ./component/gen

WORKDIR /opt/cyclo/component
RUN npm ci --omit=dev

WORKDIR /opt/cyclo
COPY provider/package.json provider/package-lock.json ./provider/
COPY provider/src ./provider/src
COPY provider/gen ./provider/gen

WORKDIR /opt/cyclo/provider
RUN npm ci --omit=dev

WORKDIR /opt/cyclo
COPY passthrough/package.json passthrough/package-lock.json ./passthrough/

WORKDIR /opt/cyclo/passthrough
RUN npm ci --omit=dev

COPY --chown=node:node passthrough/component.conf ./component.conf
COPY --chown=node:node passthrough/src ./src

RUN install -d -o node -g node -m 0770 /run/cyclo \
 && install -d -o node -g node -m 0550 /run/cyclo/requirements/upstream

ENV NODE_ENV=production

USER 1000:1000
STOPSIGNAL SIGTERM
ENTRYPOINT ["node", "src/main.mjs"]
CMD ["serve"]

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD ["node", "src/healthcheck.mjs"]
