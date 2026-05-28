# GitHub Actions CI/CD Workflows

This directory contains GitHub Actions workflows for Continuous Integration and Continuous Deployment.

## Workflows

### 1. CI Workflow (`ci.yml`)

**Trigger:** Push/PR to `main` or `develop` branches

**Jobs:**
- **Lint:** Code quality checks with ruff and black
- **Test:** Run pytest with coverage
- **Automation/Flow Regression:** Focused regression checks for action-call routing, lookup chains, interpolation, and approval paths
- **Build:** Build Docker images
- **Security Scan:** Vulnerability scanning with Trivy

**Required Secrets:** None (runs on every PR)

### 2. CD Workflow (`cd.yml`)

**Trigger:** CI success on `develop`/`main`, or manual dispatch

**Jobs:**
- **Deploy staging stack**
  - Build Docker image
  - Copy files via SCP
  - Run migrations
  - Seed pricing + staging E2E fixtures
  - Start services and verify health
- **Staging API E2E**
  - Run live pytest checks against staging API URLs
- **Dashboard Staging E2E**
  - Trigger the dashboard repo Playwright workflow against the deployed Vercel dashboard URL and wait for completion
- **Deploy production stack**
  - Runs only after staging API + dashboard E2E succeed

**Required Secrets:**
```
VPS_HOST          - Your VPS IP address or hostname
VPS_USERNAME      - SSH username (e.g., root or deploy)
VPS_SSH_KEY       - Private SSH key for authentication
VPS_PORT          - SSH port (default: 22)
DEPLOY_PATH       - Target directory for the environment on the VPS (e.g., /opt/luran-staging or /opt/luran)
APP_URL           - Your application URL (e.g., https://yourdomain.com)
API_PORT          - Host port exposed by the API on that environment (e.g., 8001 for staging, 8000 for production)
CROSS_REPO_GITHUB_TOKEN - Token that can dispatch workflows in the dashboard repo
DASHBOARD_REPO    - owner/repo slug for the dashboard repository
DASHBOARD_REF     - branch/ref to dispatch in the dashboard repo
DASHBOARD_URL     - Vercel dashboard URL for the environment under test
DOCS_URL          - public docs URL for the environment under test
LURAN_E2E_PRO_EMAIL / PASSWORD
LURAN_E2E_LITE_EMAIL / PASSWORD
LURAN_E2E_QUOTA_EMAIL / PASSWORD
LURAN_E2E_PLATFORM_EMAIL / PASSWORD
```

### 3. Docker Workflow (`docker.yml`)

**Trigger:** Push to `main`/`develop`, tags, or PRs

**Jobs:**
- **Build and Push:** Build and push Docker images to GitHub Container Registry

**Required Secrets:** None (uses `GITHUB_TOKEN` automatically)

## Setup Instructions

### 1. Configure GitHub Secrets

Use:
- **Environment secrets** for values that differ between `staging` and `production`
- **Repository secrets** only for values that are truly shared across both environments

Go to:
- **Settings → Environments** and create `staging` and `production`
- then add the environment-specific secrets there

#### For VPS Deployment:
```bash
# Generate SSH key if you don't have one
ssh-keygen -t ed25519 -C "github-actions"

# Copy public key to VPS
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@your-vps-ip

# Copy private key content to GitHub secret VPS_SSH_KEY
cat ~/.ssh/id_ed25519
```

Add these environment secrets:
- `VPS_HOST`: `your-server-ip` or `yourdomain.com`
- `VPS_USERNAME`: `deploy` (or your SSH user)
- `VPS_SSH_KEY`: Contents of your private key
- `VPS_PORT`: `22` (or your custom SSH port)
- `APP_URL`: `https://yourdomain.com`

#### For Notifications (Optional):
- `SLACK_WEBHOOK`: Slack webhook URL for deployment notifications

### 2. Prepare VPS

The current release flow assumes:
- **production** and **staging** share the same VPS
- each environment has its own **deployment directory**
- each environment has its own **`.env`**
- each environment has its own **Compose project name** and API host port
- the dashboard is hosted on **Vercel**, not this VPS

Recommended paths:
- production: `/opt/luran`
- staging: `/opt/luran-staging`

Recommended stack identity:
- production: `COMPOSE_PROJECT_NAME=luran`, `API_PORT=8000`
- staging: `COMPOSE_PROJECT_NAME=luran_staging`, `API_PORT=8001`

That means the first staging setup is a one-time bootstrap on the same server, not a second server.

SSH into your VPS and set up the deployment directory:

```bash
# Create deployment directories
sudo mkdir -p /opt/luran
sudo mkdir -p /opt/luran-staging
sudo chown $USER:$USER /opt/luran
sudo chown $USER:$USER /opt/luran-staging

# Create .env files
cd /opt/luran
nano .env
# Add your production environment variables
# CRITICAL: lock down permissions — /opt is world-readable by default
chmod 600 .env
ls -la .env

cd /opt/luran-staging
nano .env
# Add your staging environment variables

# Install Docker (includes docker compose plugin on modern installs)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

On the first staging bootstrap, also make sure:
- your reverse proxy points the staging API subdomain to the staging stack
- your staging `.env` points to staging-only database, Redis, and Qdrant resources
- your Vercel staging or preview deployment uses the staging API URL in `NEXT_PUBLIC_API_URL`
- your staging `.env` sets a different `COMPOSE_PROJECT_NAME` and `API_PORT` from production

### 3. Environment Setup

Create `/opt/luran/.env` and `/opt/luran-staging/.env` on your VPS with environment-specific values:

```bash
# Copy from .env.example and fill in production values
SECRET_KEY=<generate with: openssl rand -hex 32>
POSTGRES_PASSWORD=<strong-password>
OPENAI_API_KEY=<your-openai-api-key>
# ... other variables used by app/core/config.py
# ... other variables
```

For staging, keep the values isolated from production:
- separate database name/URL
- separate Redis instance/DB
- separate Qdrant collection/storage path
- staging API domain
- different `COMPOSE_PROJECT_NAME`
- different `API_PORT`

### 3a. GitHub Environment Setup

Use GitHub environments so the same workflow can deploy both stacks safely:

- `staging`
  - `VPS_HOST`: same production server hostname/IP
  - `DEPLOY_PATH`: `/opt/luran-staging`
  - `APP_URL`: staging API URL such as `https://staging-api.luranai.com`
  - `API_PORT`: `8001`
  - `DASHBOARD_URL`: Vercel staging/preview URL for `luran-dashboard`
- `production`
  - `VPS_HOST`: same production server hostname/IP
  - `DEPLOY_PATH`: `/opt/luran`
  - `APP_URL`: production API URL such as `https://api.luranai.com`
  - `API_PORT`: `8000`
  - `DASHBOARD_URL`: production dashboard URL if needed for release checks

### 4. Initial Deployment

```bash
# On your VPS
cd /opt/luran
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec api alembic upgrade head
```

For first-time staging bootstrap, do the same from `/opt/luran-staging` after the workflow copies the files there.

### 5. Enable Workflows

- Push to `main` branch triggers automatic deployment
- Create tags (`v1.0.0`) for versioned releases
- Use "Actions" tab to manually trigger deployment

## Workflow Features

### Deployment Behavior
- Staging deploy happens before production deploys
- Staging and production can live on the same VPS because they use different deployment paths and environment secrets
- Staging seeds deterministic E2E fixtures
- Playwright runs against the deployed Vercel dashboard URL, not a dashboard process on the VPS
- Production deploy is hard-blocked until staging API and dashboard E2E succeed
- Uses `docker compose -f docker-compose.prod.yml up -d`
- Runs migration before service refresh
- Validates deployment with health check

### Security
- No secrets in code
- SSH key-based authentication
- Vulnerability scanning with Trivy
- Image signing (can be enabled)

### Monitoring
- Deployment status notifications
- Health check validation
- Coverage reports uploaded to Codecov

## Manual Deployment

Trigger manual deployment from GitHub:

1. Go to **Actions** → **CD - Staging Gate and Deploy**
2. Click **Run workflow**
3. Select environment (production/staging)
4. Click **Run workflow**

## Rollback

Current workflow does **not** implement automatic rollback.
If deployment fails, use manual rollback.

Manual rollback:
```bash
# SSH into VPS
cd /opt/luran
docker compose -f docker-compose.prod.yml down
docker tag luran-api:previous luran-api:latest
docker compose -f docker-compose.prod.yml up -d
```

## To confirm deployment on VPS, run these once:
```bash
cd /opt/luran
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 api
curl -sS http://localhost:8000/api/v1/health
```

## Also verify running image/build time:
```bash
docker images | grep luran-api
docker inspect luran-api:latest --format='{{.Created}}'
```
### NB: If health is ok and containers are up, deployment is updated.


## Troubleshooting

### Deployment Fails
- Check VPS SSH access: `ssh user@vps-ip`
- Verify secrets are set correctly
- Check VPS logs in the right environment path:
  - staging: `cd /opt/luran-staging && docker compose -f docker-compose.yml -f docker-compose.prod.yml logs`
  - production: `cd /opt/luran && docker compose -f docker-compose.yml -f docker-compose.prod.yml logs`

### Health Check Fails
- Check API logs: `docker compose -f docker-compose.yml -f docker-compose.prod.yml logs api`
- Verify environment variables in the matching `.env`
- Test health endpoint: `curl http://localhost:8000/api/v1/health`

### Build Fails
- Check Dockerfile syntax
- Verify all dependencies in `pyproject.toml`
- Test local build: `docker build -t luran-api .`

## Best Practices

1. **Always test locally first:**
   ```bash
   docker compose -f docker-compose.prod.yml build
   docker compose -f docker-compose.prod.yml up -d
   curl http://localhost:8000/api/v1/health
   ```

2. **Use feature branches:**
   - Create PR for code review
   - CI runs automatically
   - Merge to `main` triggers deployment

3. **Version your releases:**
   ```bash
   git tag -a v1.0.0 -m "Release v1.0.0"
   git push origin v1.0.0
   ```

4. **Monitor deployments:**
   - Check Actions tab for workflow status
   - Monitor VPS resource usage
   - Set up uptime monitoring (e.g., UptimeRobot)

## Next Steps

- [ ] Enable Docker image signing
- [ ] Set up monitoring alerts (e.g., Slack, Discord)
- [ ] Configure backup automation
