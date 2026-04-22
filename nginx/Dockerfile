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
FROM nginx:alpine
COPY --from=build /app/build /usr/share/nginx/html
COPY nginx/nginx.conf.template /tmp/nginx.conf.template
RUN printf '#!/bin/sh\n\
# Load ADMIN_API_KEY from Docker secret file (preferred) to avoid exposure in\n\
# docker inspect environment output.\n\
if [ -f /run/secrets/admin_api_key ]; then\n\
    ADMIN_API_KEY=$(cat /run/secrets/admin_api_key)\n\
    export ADMIN_API_KEY\n\
fi\n\
envsubst "$$ADMIN_API_KEY" < /tmp/nginx.conf.template > /etc/nginx/conf.d/default.conf\n' \
    > /docker-entrypoint.d/90-envsubst-admin-key.sh && \
    chmod +x /docker-entrypoint.d/90-envsubst-admin-key.sh
EXPOSE 80
