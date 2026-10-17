FROM node:22-alpine AS base

# Install dependencies only when needed
FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app

# Lockfile MUST come along: without it every build resolves the tree fresh
# from the registry. On 03.09.2026 that resolution hit an npm 10.9 arborist
# bug ("Cannot read properties of null (reading 'edgesOut')") after an
# upstream release — CI and deploy broke with an unchanged repo. `npm ci`
# installs exactly what the lockfile says and fails loudly if it drifts.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# Build the application
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ENV NEXT_TELEMETRY_DISABLED=1

# PUBLIC_HOST is baked into next.config.ts at build time (server-actions
# origin allowlist) — passed as a build arg from docker-compose.yml.
ARG PUBLIC_HOST=
ENV PUBLIC_HOST=$PUBLIC_HOST
# Brand wordmark (NEXT_PUBLIC_* is baked at build time)
ARG NEXT_PUBLIC_BRAND=
ENV NEXT_PUBLIC_BRAND=$NEXT_PUBLIC_BRAND
# Server-side /api proxy of next.config.ts — its target is frozen into the
# standalone build. Only used when :3000 is opened directly (Caddy sends
# /api/* to the backend itself); inside compose the backend is `backend:8000`,
# the localhost:8000 default is for `npm run dev` on a laptop.
ARG API_PROXY_TARGET=http://backend:8000
ENV API_PROXY_TARGET=$API_PROXY_TARGET
RUN npm run build
# Every build path (CI, release.yml, local `docker compose build`) fails here
# when the browser bundle carries a hard-coded backend origin: NEXT_PUBLIC_*
# values are frozen in, and a localhost:8000 in a published image points every
# remote viewer at their own machine (finding 17.10.2026).
RUN node scripts/check-bundle-origins.mjs .next/static

# Production runner
# ── dev target ───────────────────────────────────────────────────────────────
# `docker build --target dev`: hot-reload dev server, no standalone build.
# The production runner below stays last = the default build target.
FROM deps AS dev
WORKDIR /app
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
EXPOSE 3000
CMD ["npm", "run", "dev"]

FROM base AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public

# Standalone output
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
