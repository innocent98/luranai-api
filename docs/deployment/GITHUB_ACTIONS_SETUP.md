# GitHub Actions CI/CD Setup Guide

## Overview

Your repository has 3 automated workflows:
- **CI** - Tests, linting, security scans (runs on every push/PR)
- **CD** - Auto-deploy to VPS (runs on push to main)
- **Docker** - Builds and publishes images to GitHub Container Registry

---

## 🔧 One-Time Setup (Required for Auto-Deployment)

### Step 1: Generate SSH Key for GitHub Actions

**On your local machine:**

```bash
# Generate SSH key pair
ssh-keygen -t ed25519 -C "github-actions@luran"

# When prompted for file location, save as:
# /Users/yourname/.ssh/luran_deploy_key

# Press Enter twice (no passphrase needed for automation)
```

**Output:**
```
Your identification has been saved in /Users/yourname/.ssh/luran_deploy_key
Your public key has been saved in /Users/yourname/.ssh/luran_deploy_key.pub
```

---

### Step 2: Add Public Key to VPS

**Copy the public key to your server:**

```bash
# Option A: Using ssh-copy-id (easiest)
ssh-copy-id -i ~/.ssh/luran_deploy_key.pub root@your-server-ip

# Option B: Manual method
cat ~/.ssh/luran_deploy_key.pub
# Copy the output, then SSH to server and paste into ~/.ssh/authorized_keys
```

**Test the connection:**

```bash
ssh -i ~/.ssh/luran_deploy_key root@your-server-ip
# Should login without password
```

---

### Step 3: Add GitHub Repository Secrets

**Go to your GitHub repository:**

```
https://github.com/yourusername/luran/settings/secrets/actions
```

**Click:** `New repository secret`

**Add these 4 secrets:**

---

#### Secret 1: VPS_HOST

```
Name: VPS_HOST
Value: your-server-ip-or-domain.com

Examples:
  - 192.168.1.100
  - vps.example.com
  - luran.example.com
```

---

#### Secret 2: VPS_USERNAME

```
Name: VPS_USERNAME
Value: root

Or if you're using a different user:
  - ubuntu
  - admin
  - deploy
```

---

#### Secret 3: VPS_SSH_KEY

**Get the private key:**

```bash
cat ~/.ssh/luran_deploy_key
```

**Copy the ENTIRE output** (including the header and footer):

```
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtz
c2gtZWQyNTUxOQAAACDK... (many more lines)
-----END OPENSSH PRIVATE KEY-----
```

**Paste into GitHub:**

```
Name: VPS_SSH_KEY
Value: [Paste entire private key here, including BEGIN/END lines]
```

---

#### Secret 4: VPS_PORT (Optional)

```
Name: VPS_PORT
Value: 22

Only add this if you use a custom SSH port (e.g., 2222)
Otherwise, leave it out (defaults to 22)
```

---

### Step 4: Verify Secrets Are Set

**Check your secrets page:**

```
https://github.com/yourusername/luran/settings/secrets/actions
```

**You should see:**

```
✓ VPS_HOST
✓ VPS_USERNAME
✓ VPS_SSH_KEY
✓ VPS_PORT (optional)
```

---

## ✅ Test the Setup

### Test 1: Run CI Manually

**Go to Actions tab:**

```
https://github.com/yourusername/luran/actions
```

**Click:** `CI` → `Run workflow` → `Run workflow`

**Expected:** ✅ All jobs pass (lint, test, build, security-scan)

---

### Test 2: Trigger Auto-Deploy

**Make a small change and push to main:**

```bash
# Make a test change
echo "# Test deployment" >> README.md
git add README.md
git commit -m "test: trigger auto-deploy"
git push origin main
```

**Monitor deployment:**

```
https://github.com/yourusername/luran/actions
```

**You should see:**
1. ✅ CI runs first (tests, build)
2. ✅ CD runs after (deploys to VPS)
3. ✅ Docker builds image (parallel)

**Verify on server:**

```bash
ssh root@your-server-ip
cd /opt/luran
docker-compose ps
# Should show updated containers
```

---

## 🔍 Troubleshooting

### CD Fails with "Permission denied (publickey)"

**Problem:** SSH key not added to server

**Fix:**

```bash
ssh-copy-id -i ~/.ssh/luran_deploy_key.pub root@your-server-ip
```

---

### CD Fails with "No such file or directory: /opt/luran"

**Problem:** Directory doesn't exist on server

**Fix on server:**

```bash
ssh root@your-server-ip
mkdir -p /opt/luran
cd /opt/luran
git clone https://github.com/yourusername/luran.git .
```

---

### CI Fails with "Module not found"

**Problem:** Missing dependencies

**Fix:** Check that `poetry.lock` is committed:

```bash
git add poetry.lock
git commit -m "chore: add poetry.lock"
git push
```

---

### Docker Build Fails

**Problem:** Dockerfile has syntax errors

**Fix:** Test locally first:

```bash
docker build -t luran-test .
```

---

## 📋 Quick Reference

### View Workflow Runs

```
https://github.com/yourusername/luran/actions
```

### Cancel Running Workflow

```
Actions → Click running workflow → Cancel workflow
```

### Re-run Failed Workflow

```
Actions → Click failed workflow → Re-run all jobs
```

### Update Secrets

```
Settings → Secrets and variables → Actions → Click secret name → Update secret
```

### Disable Auto-Deployment

```
.github/workflows/cd.yml → Change line 5:
branches: [main]  →  branches: [disabled]
```

---

## 🎯 What Happens on Each Push

### Push to `develop` branch:
- ✅ CI runs (tests, lint, build)
- ❌ CD does NOT run (no deployment)
- ✅ Docker builds dev image

### Push to `main` branch:
- ✅ CI runs (tests, lint, build)
- ✅ CD runs (auto-deploys to VPS)
- ✅ Docker builds production image

### Create tag `v1.0.0`:
- ✅ CI runs
- ✅ CD runs
- ✅ Docker builds versioned image (v1.0.0)

---

## 📊 Workflow Status Badges (Optional)

Add to your `README.md`:

```markdown
![CI](https://github.com/yourusername/luran/workflows/CI/badge.svg)
![CD](https://github.com/yourusername/luran/workflows/CD%20-%20Deploy%20to%20VPS/badge.svg)
```

**Replace `yourusername` with your GitHub username.**

---

## Summary

**One-time setup:**
1. ✅ Generate SSH key
2. ✅ Add public key to VPS
3. ✅ Add 4 secrets to GitHub
4. ✅ Test with a push to main

**After setup:**
- Every push to `main` → Automatic deployment to VPS
- Every PR → Automatic testing
- Zero manual deployment needed! 🚀

**Total setup time: ~10 minutes**
