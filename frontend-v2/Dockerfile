FROM node:22-alpine AS base

# Install dependencies only when needed
FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app

COPY package.json ./
RUN npm install --frozen-lockfile 2>/dev/null || npm install

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

RUN npm run build

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
