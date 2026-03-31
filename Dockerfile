# REL-8.5: Hardened production container image
# Multi-stage build with non-root user, minimal attack surface

# ---------- Stage 1: Install dependencies ----------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- Stage 2: Production image ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install runtime dependency
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN adduser --system --no-create-home --group appuser

WORKDIR /app

# Copy application code (no .env, tests, docs — see .dockerignore)
COPY app/ ./app/

# Create writable directories for non-root user
RUN mkdir -p /app/logs && chown -R appuser:appuser /app/logs \
    && chown appuser:appuser /app/app/config

RUN chown -R appuser:appuser /app/app/config

# Healthcheck for container orchestrators
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Run as non-root
USER appuser

ENV PORT=8080
EXPOSE 8080

CMD ["python", "-m", "app.main"]
