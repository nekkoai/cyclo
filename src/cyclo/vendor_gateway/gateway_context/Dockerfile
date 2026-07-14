FROM node:22-slim@sha256:53ada149d435c38b14476cb57e4a7da73c15595aba79bd6971b547ceb6d018bf

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY store.mjs usage.mjs policy.mjs pi-registry.mjs server.mjs login.mjs providers.mjs ./

# The gateway's own private credential store lives here (mount a volume).
RUN mkdir -p /var/lib/cyclo-gateway && chown node:node /var/lib/cyclo-gateway

USER node
EXPOSE 8787
# serve:  docker run <image>
# login:  docker run -it <image> login.mjs <provider> [--api-key KEY]
ENTRYPOINT ["node"]
CMD ["server.mjs"]
