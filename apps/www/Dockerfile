# MEMPAS frontend — production image
# Multi-stage: 1) Vite build with npm, 2) nginx serving the dist + proxying /api.

# ─── Stage 1: build ───────────────────────────────────────────────────────
FROM node:20-alpine AS build

WORKDIR /app

# Cache dependency install separately from source changes
COPY apps/www/package.json apps/www/package-lock.json* ./
RUN npm ci --prefer-offline --no-audit --no-fund

COPY apps/www/ ./
RUN npm run build


# ─── Stage 2: nginx runtime ───────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

# Replace the default config with our SPA + reverse-proxy config
COPY --from=build /app/dist /usr/share/nginx/html
COPY apps/www/nginx.conf /etc/nginx/conf.d/default.conf

# Healthcheck on the served index
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/ || exit 1

EXPOSE 80
