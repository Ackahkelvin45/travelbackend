# ============================================================
# Travel & Tour Backend — Optimized Multi-Stage Dockerfile
# ============================================================

# ---- Base image --------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- Stage 1: Builder (Install dependencies) ---------------
FROM base AS builder

# Install build dependencies for psycopg2, Pillow, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        libpq-dev \
        libjpeg-dev \
        zlib1g-dev \
        libwebp-dev \
        libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install dependencies into a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# ---- Stage 2: Development ----------------------------------
FROM base AS development

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libjpeg62-turbo \
        libwebp7 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy source code
COPY . .

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# ---- Stage 3: Production -----------------------------------
FROM base AS production

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libjpeg62-turbo \
        libwebp7 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -g 1001 appgroup && \
    useradd -u 1001 -g appgroup -s /bin/bash -m appuser

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy source code
COPY --chown=appuser:appgroup . .

# Prepare static/media directories
RUN mkdir -p /app/media /app/staticfiles && \
    chown -R appuser:appgroup /app/media /app/staticfiles

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/health/ || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info"]
