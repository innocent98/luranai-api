# Luran WhatsApp sidecar. Build context: ./whatsapp-sidecar
FROM node:22.23.4-slim

ENV NODE_ENV=production \
    NPM_CONFIG_UPDATE_NOTIFIER=false

WORKDIR /app

# Lockfile-exact install, no lifecycle scripts (supply-chain hygiene; see docs/sop/whatsapp_sidecar.md).
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev && npm cache clean --force

COPY src ./src

# Node 20 slim ships a `node` user (uid 1000); the process never needs root and has no writable state on disk.
USER node

# Bind inside the container only; the compose override publishes NO port. Reach it as http://whatsapp-sidecar:8787 on the compose network.
ENV LISTEN_HOST=0.0.0.0 PORT=8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+process.env.PORT+'/health',{headers:{authorization:'Bearer '+process.env.WHATSAPP_SIDECAR_TOKEN}}).then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "src/index.mjs"]
