FROM python:3.11-slim as builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
ENV POETRY_HOME="/opt/poetry"
ENV POETRY_VERSION=1.8.4
RUN curl -sSL https://install.python-poetry.org | python3 - && \
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

# Copy application
COPY ./app /app/app
COPY ./worker.py /app/worker.py
COPY ./alembic /app/alembic
COPY ./alembic.ini /app/alembic.ini
# Keep production image lean: include only scripts needed in production ops.
COPY ./scripts/seed_data.py /app/scripts/seed_data.py
COPY ./scripts/seed_flow_templates.py /app/scripts/seed_flow_templates.py
COPY ./scripts/create_luran_support_flows.py /app/scripts/create_luran_support_flows.py
COPY ./scripts/create_luran_support_flows_part2.py /app/scripts/create_luran_support_flows_part2.py
COPY ./scripts/smoke_automation_and_flows.py /app/scripts/smoke_automation_and_flows.py
COPY ./scripts/backup_db.sh /app/scripts/backup_db.sh
COPY ./scripts/backup_status.sh /app/scripts/backup_status.sh

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
    --keep-alive 5
