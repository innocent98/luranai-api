# Luran

A FastAPI project

## Features

- FastAPI framework with async support
- Poetry for dependency management
- Pydantic v2 for data validation
- SQLAlchemy 2.0 for database ORM
- Alembic for database migrations
- JWT authentication
- Docker and docker-compose support
- Comprehensive testing setup with pytest
- Pre-commit hooks for code quality
- GitHub Actions CI/CD
- Structured logging with Loguru
- API documentation with Swagger UI and ReDoc

## Requirements

- Python 3.11+
- Poetry 1.8+
- PostgreSQL (if using database)
- Redis (if using caching)
- Docker & Docker Compose (optional)

## Project Structure

```
luran/
├── app/
│   ├── api/
│   │   ├── v1/
│   │   │   ├── endpoints/
│   │   │   └── api.py
│   │   └── deps.py
│   ├── core/
│   │   ├── config.py
│   │   ├── security.py
│   │   └── logger.py
│   ├── db/
│   │   ├── models/
│   │   ├── base.py
│   │   └── session.py
│   ├── schemas/
│   ├── services/
│   ├── utils/
│   ├── middleware/
│   └── main.py
├── tests/
│   ├── api/
│   └── services/
├── alembic/
│   └── versions/
├── scripts/
├── docs/
├── .github/
│   └── workflows/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

## Getting Started

### 1. Clone and Setup

```bash
cd luran
cp .env.example .env
# Edit .env with your configuration
```

### 2. Install Poetry (if not already installed)

```bash
curl -sSL https://install.python-poetry.org | python3 -
```

### 3. Install Dependencies

```bash
# Install all dependencies (including dev)
poetry install

# Or install only production dependencies
poetry install --only main
```

### 4. Activate Virtual Environment

```bash
# Option 1: Activate the virtual environment
poetry shell

# Option 2: Run commands with poetry run prefix
poetry run python ...
```

### 5. Database Setup

```bash
# Run migrations
poetry run alembic upgrade head

# Create initial data (if needed)
poetry run python scripts/init_db.py
```

### 6. Run Development Server

```bash
# Using poetry run
poetry run uvicorn app.main:app --reload

# Or if inside poetry shell
uvicorn app.main:app --reload

# Or using the defined script
poetry run start
```

The API will be available at:
- API: http://localhost:8000
- Swagger UI: http://localhost:8000/api/v1/docs
- ReDoc: http://localhost:8000/api/v1/redoc

### 7. Using Docker

```bash
# Build and run
docker-compose up --build

# Run in background
docker-compose up -d

# View logs
docker-compose logs -f

# Stop containers
docker-compose down
```

## Development

### Running Tests

```bash
# Run all tests
poetry run pytest

# Run with coverage
poetry run pytest --cov=app --cov-report=html

# Run specific test file
poetry run pytest tests/test_health.py

# Run with verbose output
poetry run pytest -v
```

### Automation And Flow Smoke Regression

Use this when you want a live end-to-end check of:
- AI chat automation success and failure
- lookup chaining
- approval-required action escalation
- flow `action_call` execution and saved output mapping

```bash
# Recommended local command
make smoke-automation-flows

# Or run directly inside the API container
docker exec luran_api python -u /app/scripts/smoke_automation_and_flows.py
```

You can override the smoke target without editing the script:

```bash
docker exec \
  -e LURAN_SMOKE_EMAIL=tenant@example.com \
  -e LURAN_SMOKE_PASSWORD='secret' \
  -e LURAN_SMOKE_TENANT_ID=00000000-0000-0000-0000-000000000000 \
  -e LURAN_SMOKE_ORDER_FLOW_NAME='Automation Flow Smoke Success e00be652' \
  -e LURAN_SMOKE_SUFFIX=ci \
  luran_api \
  python -u /app/scripts/smoke_automation_and_flows.py
```

Supported environment variables:
- `LURAN_SMOKE_BASE_URL`
- `LURAN_SMOKE_TENANT_ID`
- `LURAN_SMOKE_EMAIL`
- `LURAN_SMOKE_PASSWORD`
- `LURAN_SMOKE_ORDER_FLOW_NAME`
- `LURAN_SMOKE_TIMEOUT_SECONDS`
- `LURAN_SMOKE_SUFFIX`

### Real-Account Enforcement E2E

Use this when you want to verify the live API with a real tenant admin account and confirm:
- subscription downgrade validation
- lookup-chain gating
- automation action count limits
- automation execution limits
- webhook count limits

```bash
# Recommended local command
make e2e-real-tenant-enforcement

# Or run directly inside the API container
docker exec -w /app -e PYTHONPATH=/app luran_api python -u scripts/e2e_real_tenant_enforcement.py
```

You can override the real-account E2E target without editing the script:

```bash
docker exec \
  -w /app \
  -e PYTHONPATH=/app \
  -e LURAN_E2E_EMAIL=tenant@example.com \
  -e LURAN_E2E_PASSWORD='secret' \
  -e LURAN_E2E_BASE_URL=http://localhost:8000/api/v1 \
  luran_api \
  python -u scripts/e2e_real_tenant_enforcement.py
```

## Staging E2E Fixtures

Create deterministic staging/test tenants and automation fixtures:

```bash
make seed-staging-e2e
```

Seeded tenant admins:
- `staging-e2e-pro@luranai.com`
- `staging-e2e-lite@luranai.com`
- `staging-e2e-quota@luranai.com`

Shared password:
- `StagingE2E123$`

## Staging Release Model

The current release flow assumes:
- the backend uses a **staging stack on the same VPS as production**
- staging and production live in **different deployment directories**
- the dashboard is hosted on **Vercel**
- Playwright runs against the **deployed Vercel URL**, while the dashboard itself points to the staging API URL

Recommended first-time server bootstrap:
- production path: `/opt/luran`
- staging path: `/opt/luran-staging`
- separate `.env` file in each path
- `COMPOSE_PROJECT_NAME=luran` for production and `COMPOSE_PROJECT_NAME=luran_staging` for staging
- `API_PORT=8000` for production and `API_PORT=8001` for staging
- separate staging database/Redis/Qdrant resources or clearly isolated equivalents

That means yes: for this release gate to work, the first time you set it up you need to create the staging directory and staging environment on the existing server.

## Live API E2E Suite

Run the pytest-based live endpoint checks:

```bash
make test-e2e-live
```

Required environment variables for CI or manual runs:

```bash
LURAN_E2E_BASE_URL=https://staging-api.example.com/api/v1
LURAN_E2E_PRO_EMAIL=staging-e2e-pro@luranai.com
LURAN_E2E_PRO_PASSWORD=StagingE2E123$
LURAN_E2E_LITE_EMAIL=staging-e2e-lite@luranai.com
LURAN_E2E_LITE_PASSWORD=StagingE2E123$
LURAN_E2E_QUOTA_EMAIL=staging-e2e-quota@luranai.com
LURAN_E2E_QUOTA_PASSWORD=StagingE2E123$
```

Supported environment variables:
- `LURAN_E2E_BASE_URL`
- `LURAN_E2E_EMAIL`
- `LURAN_E2E_PASSWORD`
- `LURAN_E2E_TIMEOUT_SECONDS`

### Code Quality

```bash
# Format code
poetry run black app tests
poetry run isort app tests

# Lint code
poetry run ruff check app tests
poetry run mypy app

# Run all checks
poetry run pre-commit run --all-files

# Install pre-commit hooks
poetry run pre-commit install
```

### Database Migrations

```bash
# Create a new migration
poetry run alembic revision --autogenerate -m "description"

# Apply migrations
poetry run alembic upgrade head

# Rollback migration
poetry run alembic downgrade -1

# View migration history
poetry run alembic history
```

### Adding Dependencies

```bash
# Add a production dependency
poetry add package-name

# Add a dev dependency
poetry add --group dev package-name

# Update dependencies
poetry update

# Show dependency tree
poetry show --tree
```

## Environment Variables

See `.env.example` for all available environment variables.

Key variables:
- `SECRET_KEY`: Secret key for JWT tokens (generate with `openssl rand -hex 32`)
- `DATABASE_URL`: PostgreSQL connection string
- `REDIS_URL`: Redis connection string
- `ENVIRONMENT`: development/staging/production

## API Documentation

Once the server is running, visit:
- Swagger UI: http://localhost:8000/api/v1/docs
- ReDoc: http://localhost:8000/api/v1/redoc

## Deployment

### Using Docker

```bash
# Build production image
docker build -t luran:latest .

# Run container
docker run -p 8000:8000 --env-file .env luran:latest
```

### Manual Deployment

```bash
# Install production dependencies only
poetry install --only main

# Run with gunicorn
poetry run gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000
```

## Contributing

1. Create a feature branch
2. Make your changes
3. Run tests and linting
4. Submit a pull request

## License

MIT License

## Author

Your Name (your.email@example.com)
