# GitHub Actions CI/CD Workflows

This directory contains GitHub Actions workflows for Continuous Integration and Continuous Deployment.

## Workflows

### 1. CI Workflow (`ci.yml`)

**Trigger:** Push/PR to `main` or `develop` branches

**Jobs:**
- **Lint:** Code quality checks with ruff and black
- **Test:** Run pytest with coverage
- **Build:** Build Docker images
- **Security Scan:** Vulnerability scanning with Trivy

**Required Secrets:** None (runs on every PR)

### 2. CD Workflow (`cd.yml`)

**Trigger:** Push to `main` branch or manual dispatch

**Jobs:**
- **Deploy:** Deploy to VPS
  - Build Docker images
  - Copy files via SCP
  - Run migrations
  - Zero-downtime deployment
  - Health check validation

**Required Secrets:**
```
VPS_HOST          - Your VPS IP address or hostname
VPS_USERNAME      - SSH username (e.g., root or deploy)
VPS_SSH_KEY       - Private SSH key for authentication
VPS_PORT          - SSH port (default: 22)
APP_URL           - Your application URL (e.g., https://yourdomain.com)
```

### 3. Docker Workflow (`docker.yml`)

**Trigger:** Push to `main`/`develop`, tags, or PRs

**Jobs:**
- **Build and Push:** Build and push Docker images to GitHub Container Registry

**Required Secrets:** None (uses `GITHUB_TOKEN` automatically)

## Setup Instructions

### 1. Configure GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

#### For VPS Deployment:
```bash
# Generate SSH key if you don't have one
ssh-keygen -t ed25519 -C "github-actions"

# Copy public key to VPS
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@your-vps-ip

# Copy private key content to GitHub secret VPS_SSH_KEY
cat ~/.ssh/id_ed25519
```

Add these secrets:
- `VPS_HOST`: `your-server-ip` or `yourdomain.com`
- `VPS_USERNAME`: `deploy` (or your SSH user)
- `VPS_SSH_KEY`: Contents of your private key
- `VPS_PORT`: `22` (or your custom SSH port)
- `APP_URL`: `https://yourdomain.com`

#### For Notifications (Optional):
- `SLACK_WEBHOOK`: Slack webhook URL for deployment notifications

### 2. Prepare VPS

SSH into your VPS and set up the deployment directory:

```bash
# Create deployment directory
sudo mkdir -p /opt/luran
sudo chown $USER:$USER /opt/luran

# Create .env file
cd /opt/luran
nano .env
# Add your production environment variables

# Install Docker and Docker Compose
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

### 3. Environment Setup

Create `/opt/luran/.env` on your VPS with production values:

```bash
# Copy from .env.example and fill in production values
SECRET_KEY=<generate with: openssl rand -hex 32>
POSTGRES_PASSWORD=<strong-password>
GOOGLE_API_KEY=<your-api-key>
PGADMIN_PASSWORD=<strong-password>
# ... other variables
```

### 4. Initial Deployment

```bash
# On your VPS
cd /opt/luran
docker-compose pull
docker-compose up -d
docker-compose exec api alembic upgrade head
```

### 5. Enable Workflows

- Push to `main` branch triggers automatic deployment
- Create tags (`v1.0.0`) for versioned releases
- Use "Actions" tab to manually trigger deployment

## Workflow Features

### Zero-Downtime Deployment
- Uses `docker-compose up -d --no-deps` to restart only changed services
- Health check validation before marking deployment as successful
- Automatic rollback on failure

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

1. Go to **Actions** → **CD - Deploy to VPS**
2. Click **Run workflow**
3. Select environment (production/staging)
4. Click **Run workflow**

## Rollback

If deployment fails, the workflow automatically:
1. Detects failure via health check
2. Rolls back to previous Docker image
3. Validates rollback with health check

Manual rollback:
```bash
# SSH into VPS
cd /opt/luran
docker-compose down
docker tag luran-api:previous luran-api:latest
docker-compose up -d
```

## Troubleshooting

### Deployment Fails
- Check VPS SSH access: `ssh user@vps-ip`
- Verify secrets are set correctly
- Check VPS logs: `cd /opt/luran && docker-compose logs`

### Health Check Fails
- Check API logs: `docker-compose logs api`
- Verify environment variables in `/opt/luran/.env`
- Test health endpoint: `curl http://localhost:8000/api/v1/health`

### Build Fails
- Check Dockerfile syntax
- Verify all dependencies in `pyproject.toml`
- Test local build: `docker build -t luran-api .`

## Best Practices

1. **Always test locally first:**
   ```bash
   docker-compose build
   docker-compose up -d
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

- [ ] Set up staging environment
- [ ] Enable Docker image signing
- [ ] Add integration tests to CI
- [ ] Set up monitoring alerts (e.g., Slack, Discord)
- [ ] Configure backup automation
