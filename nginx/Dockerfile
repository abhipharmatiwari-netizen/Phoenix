# Stage 1: Build React app
FROM node:18-alpine AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci || npm install; else npm install; fi
COPY frontend/ .
# Bake sensible defaults so fresh browsers work without manual tenant entry.
# REACT_APP_BACKEND_URL is left empty — the client auto-detects same-origin via nginx.
ENV REACT_APP_TENANT_ID=tenant-1
RUN npm run build

# Stage 2: Serve with nginx
FROM nginx:1.29.3-trixie
RUN apt-get update && \
    apt-get upgrade -y --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*
COPY --from=build /app/build /usr/share/nginx/html
COPY nginx/nginx.conf.template /tmp/nginx.conf.template
RUN { \
    echo '#!/bin/sh'; \
    echo '# Load ADMIN_API_KEY from Docker secret file to avoid exposure in docker inspect.'; \
    echo 'if [ -f /run/secrets/admin_api_key ]; then'; \
    echo '    ADMIN_API_KEY=$(cat /run/secrets/admin_api_key)'; \
    echo '    export ADMIN_API_KEY'; \
    echo 'fi'; \
    echo 'if [ -z "${ADMIN_API_KEY:-}" ]; then'; \
    echo '    echo "ADMIN_API_KEY is required for nginx /metrics protection" >&2'; \
    echo '    exit 1'; \
    echo 'fi'; \
    echo '# PHOENIX_DOMAIN is required for the SSL cert path in nginx-ssl.conf.template.'; \
    echo '# It is passed as an environment variable from the compose stack.'; \
    echo 'if [ -z "${PHOENIX_DOMAIN:-}" ]; then'; \
    echo '    echo "PHOENIX_DOMAIN is not set — SSL cert path will not be substituted" >&2'; \
    echo 'fi'; \
    echo 'envsubst '\''${ADMIN_API_KEY} ${PHOENIX_DOMAIN}'\'' < /tmp/nginx.conf.template > /etc/nginx/conf.d/default.conf'; \
    } > /docker-entrypoint.d/90-envsubst-admin-key.sh && \
    chmod +x /docker-entrypoint.d/90-envsubst-admin-key.sh
EXPOSE 80 8443
