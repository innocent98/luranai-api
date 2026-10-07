FROM python:3.11-slim as builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
ENV POETRY_HOME="/opt/poetry"
ENV POETRY_VERSION=2.2.1
RUN curl -sSL https://install.python-poetry.org | python3 - --version "${POETRY_VERSION}" && \
    ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry

# Copy dependency files
COPY pyproject.toml poetry.lock* ./

# Install dependencies (no dev dependencies for production)
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --only main

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Install Playwright's Chromium browser + its OS libraries for the website
# auditor. The playwright pip package (installed in the builder stage) ships
# only the Python client and Node driver; the browser binary is a separate
# ~110MB download. Without this the auditor's render_page() cannot launch a
# browser, silently scans 0 pages, and reports a misleading 0% score.
# PLAYWRIGHT_BROWSERS_PATH pins a shared, root-owned, world-readable location so
# the non-root appuser can launch the browser at runtime (the default per-user
# cache would be empty for appuser). Must run as root, before USER appuser.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install --with-deps chromium && \
    chmod -R a+rx /opt/ms-playwright

# Copy application
COPY ./app /app/app
COPY ./worker.py /app/worker.py
COPY ./alembic /app/alembic
COPY ./alembic.ini /app/alembic.ini
# Keep production image lean: include only scripts needed in production ops.
COPY ./scripts/seed_data.py /app/scripts/seed_data.py
COPY ./scripts/seed_staging_e2e.py /app/scripts/seed_staging_e2e.py
COPY ./scripts/seed_flow_templates.py /app/scripts/seed_flow_templates.py
COPY ./scripts/create_luran_support_flows.py /app/scripts/create_luran_support_flows.py
COPY ./scripts/create_luran_support_flows_part2.py /app/scripts/create_luran_support_flows_part2.py
COPY ./scripts/smoke_automation_and_flows.py /app/scripts/smoke_automation_and_flows.py
COPY ./scripts/e2e_real_tenant_enforcement.py /app/scripts/e2e_real_tenant_enforcement.py
COPY ./scripts/backup_status.sh /app/scripts/backup_status.sh
COPY ./scripts/generate_encryption_key.py /app/scripts/generate_encryption_key.py
COPY ./scripts/backfill_encrypt_secrets.py /app/scripts/backfill_encrypt_secrets.py
COPY ./scripts/backfill_encrypt_payments.py /app/scripts/backfill_encrypt_payments.py
COPY ./scripts/backfill_encrypt_messages.py /app/scripts/backfill_encrypt_messages.py
COPY ./scripts/backfill_encrypt_pii.py /app/scripts/backfill_encrypt_pii.py
COPY ./scripts/backfill_encrypt_user_admin.py /app/scripts/backfill_encrypt_user_admin.py

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD gunicorn app.main:app \
    --workers ${API_WORKERS:-4} \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 \
    --access-logfile - \
    --error-logfile - \
    --log-level ${LOG_LEVEL:-info} \
    --timeout 120 \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --max-requests 1000 \
    --max-requests-jitter 200
