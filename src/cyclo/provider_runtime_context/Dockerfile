FROM node:22-slim@sha256:53ada149d435c38b14476cb57e4a7da73c15595aba79bd6971b547ceb6d018bf

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY safe-model-fields.json metadata.mjs protocol.mjs server.mjs ./
RUN mkdir -p /etc/cyclo

USER node
ENTRYPOINT ["node", "/app/server.mjs"]
