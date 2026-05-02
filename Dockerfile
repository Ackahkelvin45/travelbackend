# ============================================================
# Travel & Tour Backend — Multi-Stage Dockerfile
# ============================================================
# Stage 1  → python-deps   : install Python packages into a venv
# Stage 2  → development   : dev server with hot-reload (volume mount)
# Stage 3  → production    : hardened, non-root, minimal runtime image
# ============================================================

# ---- Base image shared by all stages -----------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- Stage 1: Dependency installation ----------------------
FROM base AS python-deps

# Install build tools needed by some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements file first — maximises layer caching
COPY requirements.txt ./

# Install into an isolated venv so we can copy it cleanly later
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install -r requirements.txt && \
    /opt/venv/bin/pip install gunicorn psycopg2-binary python-dotenv whitenoise

ENV PATH="/opt/venv/bin:$PATH"

# ---- Stage 2: Development ----------------------------------
FROM base AS development

# Runtime deps (libpq for psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Pull in the pre-built venv
COPY --from=python-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy project source (volume-mounted in docker-compose so changes
# are reflected immediately without rebuilding the image)
COPY . .

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# ---- Stage 3: Production -----------------------------------
FROM base AS production

# Minimal runtime deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with explicit UID/GID
RUN groupadd -g 1001 appgroup && \
    useradd -u 1001 -g appgroup -s /bin/bash -m appuser

# Pull in the pre-built venv
COPY --from=python-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy project source with correct ownership
COPY --chown=appuser:appgroup . .

# Create directories that must be writable at runtime
RUN mkdir -p /app/media /app/staticfiles && \
    chown -R appuser:appgroup /app/media /app/staticfiles

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/health/ || exit 1

ENTRYPOINT ["./entrypoint.sh"]
# Gunicorn: 2 workers per CPU core is the standard starting point
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info"]
