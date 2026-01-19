# Luran Widget CDN - Deployment Guide

Complete guide for deploying the widget CDN to different hosting platforms.

## Table of Contents

1. [Option A: Vercel (Recommended)](#option-a-vercel-recommended)
2. [Option B: Netlify](#option-b-netlify)
3. [Option C: Cloudflare Pages](#option-c-cloudflare-pages)
4. [DNS Configuration](#dns-configuration)
5. [Testing Deployment](#testing-deployment)

---

## Option A: Vercel (Recommended)

### Why Vercel?

- ✅ Best for Next.js (if using for chat.luranai.com)
- ✅ 190+ edge locations globally
- ✅ Simple CLI deployment
- ✅ Automatic SSL certificates
- ✅ Free tier sufficient for millions of requests
- ✅ Git integration

### Prerequisites

```bash
# Install Vercel CLI
npm install -g vercel

# Or using Homebrew
brew install vercel-cli
```

### Step 1: Initial Deployment

```bash
# Navigate to cdn-widget directory
cd /path/to/luran/cdn-widget

# Login to Vercel
vercel login

# Deploy to production
vercel --prod
```

**Follow the prompts:**
```
? Set up and deploy "~/luran/cdn-widget"? [Y/n] Y
? Which scope do you want to deploy to? Your Name
? Link to existing project? [y/N] N
? What's your project's name? luran-widget-cdn
? In which directory is your code located? ./
? Want to override the settings? [y/N] N
```

### Step 2: Add Custom Domain

**Via Vercel CLI:**
```bash
vercel domains add cdn.luranai.com
```

**Via Dashboard:**
1. Go to https://vercel.com/dashboard
2. Select `luran-widget-cdn` project
3. Go to Settings → Domains
4. Click "Add Domain"
5. Enter: `cdn.luranai.com`
6. Click "Add"

### Step 3: Configure DNS (See DNS Configuration section)

### Step 4: Verify Deployment

```bash
# Test widget loading
curl -I https://cdn.luranai.com/widget/v1/widget.js

# Expected response:
# HTTP/2 200
# content-type: application/javascript; charset=utf-8
# cache-control: public, max-age=31536000, immutable
```

### Step 5: Set Up Git Integration

```bash
# Initialize git repository
git init
git add .
git commit -m "Initial CDN setup"

# Create GitHub repository
# Then push:
git remote add origin https://github.com/yourusername/luran-widget-cdn.git
git push -u origin main

# Link to Vercel
vercel git connect
```

**Now every push to main branch automatically deploys to production!**

### Updating Widget

```bash
# Update widget.js
vim public/widget/v1/widget.js

# Commit and push
git add public/widget/v1/widget.js
git commit -m "Update widget v1"
git push

# Vercel auto-deploys (takes ~30 seconds)
```

---

## Option B: Netlify

### Why Netlify?

- ✅ Simple drag-and-drop deployment
- ✅ Great free tier
- ✅ Good global CDN
- ✅ Easy DNS management

### Prerequisites

```bash
# Install Netlify CLI
npm install -g netlify-cli

# Or using Homebrew
brew install netlify-cli
```

### Step 1: Initial Deployment

**Method 1: CLI**
```bash
# Navigate to cdn-widget directory
cd /path/to/luran/cdn-widget

# Login to Netlify
netlify login

# Initialize and deploy
netlify init

# Follow prompts:
? What would you like to do? Create & configure a new site
? Team: Your Team
? Site name: luran-widget-cdn
? Publish directory: public

# Deploy to production
netlify deploy --prod --dir=public
```

**Method 2: Drag and Drop**
1. Go to https://app.netlify.com/drop
2. Drag the `public` folder
3. Wait for deployment
4. Note the URL (e.g., `random-name-123.netlify.app`)

### Step 2: Add Custom Domain

**Via Netlify CLI:**
```bash
netlify domains:add cdn.luranai.com
```

**Via Dashboard:**
1. Go to Site Settings → Domain Management
2. Click "Add custom domain"
3. Enter: `cdn.luranai.com`
4. Click "Verify"
5. Follow DNS configuration instructions

### Step 3: Configure DNS (See DNS Configuration section)

### Step 4: Enable HTTPS

Netlify automatically provisions SSL certificates via Let's Encrypt.
Wait 5-10 minutes after DNS propagation.

### Step 5: Set Up Git Integration

```bash
# Initialize git
git init
git add .
git commit -m "Initial CDN setup"

# Create GitHub repository and push
git remote add origin https://github.com/yourusername/luran-widget-cdn.git
git push -u origin main

# Link to Netlify
netlify link
```

---

## Option C: Cloudflare Pages

### Why Cloudflare Pages?

- ✅ Fastest CDN (more edge locations than Vercel/Netlify)
- ✅ Unlimited bandwidth (even on free tier)
- ✅ Built-in DDoS protection
- ✅ Best analytics
- ✅ Free tier very generous

### Prerequisites

- Cloudflare account
- Domain managed by Cloudflare (or willing to transfer DNS)

### Step 1: Create Pages Project

**Via Dashboard:**
1. Go to https://dash.cloudflare.com
2. Select your account
3. Go to Workers & Pages → Pages
4. Click "Create application"
5. Click "Connect to Git" OR "Direct Upload"

**For Git Integration:**
1. Connect GitHub account
2. Select repository
3. Configure build:
   - Build command: (leave empty)
   - Build output directory: `public`
4. Click "Save and Deploy"

**For Direct Upload:**
```bash
# Install Wrangler CLI
npm install -g wrangler

# Login
wrangler login

# Deploy
cd /path/to/luran/cdn-widget
wrangler pages publish public --project-name=luran-widget-cdn
```

### Step 2: Add Custom Domain

1. In Pages project → Custom Domains
2. Click "Set up a custom domain"
3. Enter: `cdn.luranai.com`
4. Click "Continue"
5. Cloudflare automatically creates DNS records (if domain is on Cloudflare)

### Step 3: Verify Deployment

```bash
curl -I https://cdn.luranai.com/widget/v1/widget.js
```

---

## DNS Configuration

### If Using Cloudflare

**Automatic (Recommended):**
Cloudflare automatically creates records when you add custom domain to Pages.

**Manual:**
```
Type: CNAME
Name: cdn
Target: luran-widget-cdn.pages.dev
Proxy: Enabled (orange cloud)
```

### If Using Vercel

**Add to your DNS provider:**
```
Type: CNAME
Name: cdn
Value: cname.vercel-dns.com
TTL: 3600
```

**Verify DNS:**
```bash
dig cdn.luranai.com

# Should show CNAME to cname.vercel-dns.com
```

### If Using Netlify

**Add to your DNS provider:**
```
Type: CNAME
Name: cdn
Value: luran-widget-cdn.netlify.app
TTL: 3600
```

**Or use Netlify DNS (easier):**
1. Go to Domain Management → Netlify DNS
2. Click "Use Netlify DNS"
3. Update nameservers at your registrar
4. Netlify handles everything automatically

### DNS Propagation

- **Local:** 5-15 minutes
- **Global:** 30-60 minutes
- **Maximum:** 48 hours

**Check propagation:**
```bash
# Check from your location
nslookup cdn.luranai.com

# Check globally
https://www.whatsmydns.net/#CNAME/cdn.luranai.com
```

---

## Testing Deployment

### 1. Basic Connectivity

```bash
# Test if widget loads
curl https://cdn.luranai.com/widget/v1/widget.js

# Should return JavaScript code
```

### 2. Headers Check

```bash
# Check CORS headers
curl -I https://cdn.luranai.com/widget/v1/widget.js | grep -i cors

# Expected: access-control-allow-origin: *

# Check cache headers
curl -I https://cdn.luranai.com/widget/v1/widget.js | grep -i cache

# Expected: cache-control: public, max-age=31536000, immutable
```

### 3. Test from Browser

```html
<!DOCTYPE html>
<html>
<head><title>Widget Test</title></head>
<body>
  <h1>Testing Widget Loading</h1>
  <script>
    window.luranSettings = {
      tenantId: 'test-tenant-uuid',
      apiKey: 'pk_live_test'
    };
  </script>
  <script src="https://cdn.luranai.com/widget/v1/widget.js"></script>
  <script>
    console.log('Widget loaded:', window.LuranWidget);
  </script>
</body>
</html>
```

Open browser console and verify:
- ✅ No CORS errors
- ✅ Widget JavaScript loads
- ✅ `window.LuranWidget` object exists

### 4. Performance Test

```bash
# Test from multiple locations
curl -w "@curl-format.txt" -o /dev/null -s https://cdn.luranai.com/widget/v1/widget.js

# Create curl-format.txt:
cat > curl-format.txt << EOF
    time_namelookup:  %{time_namelookup}\n
       time_connect:  %{time_connect}\n
    time_appconnect:  %{time_appconnect}\n
      time_redirect:  %{time_redirect}\n
   time_starttransfer:  %{time_starttransfer}\n
                     ----------\n
         time_total:  %{time_total}\n
EOF
```

**Expected times:**
- DNS lookup: < 50ms
- Connection: < 100ms
- First byte: < 200ms
- Total: < 300ms

### 5. Cache Test

```bash
# First request (cache MISS)
curl -I https://cdn.luranai.com/widget/v1/widget.js

# Second request (cache HIT)
curl -I https://cdn.luranai.com/widget/v1/widget.js

# Look for cache status headers:
# Vercel: x-vercel-cache: HIT
# Cloudflare: cf-cache-status: HIT
# Netlify: x-nf-request-id: (no explicit cache header)
```

---

## Deployment Checklist

### Pre-Deployment

- [ ] Widget.js file tested locally
- [ ] All configuration files present (vercel.json, etc.)
- [ ] Git repository initialized
- [ ] .gitignore configured

### Deployment

- [ ] Deployed to hosting platform
- [ ] Custom domain added
- [ ] DNS configured
- [ ] SSL certificate active (automatic)

### Post-Deployment

- [ ] Widget loads from CDN URL
- [ ] CORS headers present
- [ ] Cache headers correct
- [ ] No console errors
- [ ] Test from multiple browsers
- [ ] Git integration working (if applicable)

---

## Rollback Procedure

### Vercel

```bash
# List deployments
vercel ls

# Rollback to specific deployment
vercel rollback <deployment-url>
```

**Or via dashboard:**
1. Go to Deployments
2. Find previous working deployment
3. Click "..." → "Promote to Production"

### Netlify

```bash
# List deploys
netlify deploy:list

# Rollback to specific deploy
netlify rollback --deploy-id=<deploy-id>
```

### Cloudflare Pages

1. Go to Deployments
2. Find previous working deployment
3. Click "Manage deployment" → "Rollback to this deployment"

---

## Cost Estimates

### Vercel

**Free Tier:**
- 100GB bandwidth/month
- Unlimited requests
- Perfect for MVP

**Pro ($20/month):**
- Unlimited bandwidth
- Unlimited requests
- Priority support

### Netlify

**Free Tier:**
- 100GB bandwidth/month
- Sufficient for widget CDN

**Pro ($19/month):**
- 400GB bandwidth
- Better analytics

### Cloudflare Pages

**Free Tier:**
- Unlimited bandwidth
- Unlimited requests
- No credit card required

**Paid Plans:**
- Not needed for CDN hosting

---

## Troubleshooting

### Widget Not Loading

1. **Check DNS:**
   ```bash
   nslookup cdn.luranai.com
   ```

2. **Check SSL:**
   ```bash
   curl -I https://cdn.luranai.com
   ```

3. **Check CORS:**
   ```bash
   curl -I https://cdn.luranai.com/widget/v1/widget.js | grep -i cors
   ```

### CORS Errors

**Problem:** Browser console shows CORS error

**Solution:**
- Verify headers configuration in vercel.json/netlify.toml/_headers
- Redeploy after changing headers
- Clear browser cache (Cmd+Shift+R)

### DNS Not Resolving

**Problem:** `cdn.luranai.com` doesn't resolve

**Solutions:**
1. Wait 30-60 minutes for DNS propagation
2. Verify CNAME record is correct
3. Check DNS at: https://www.whatsmydns.net
4. Clear local DNS cache:
   ```bash
   # macOS
   sudo dscacheutil -flushcache
   sudo killall -HUP mDNSResponder

   # Linux
   sudo systemd-resolve --flush-caches

   # Windows
   ipconfig /flushdns
   ```

### Cache Issues

**Problem:** Widget updates not showing

**Solutions:**
1. Force hard refresh: Cmd+Shift+R (Mac) or Ctrl+Shift+R (Windows)
2. Purge CDN cache (platform dashboard)
3. Check if using versioned URL: `/widget/v1/widget.js`

---

## Support

- **Vercel Docs:** https://vercel.com/docs
- **Netlify Docs:** https://docs.netlify.com
- **Cloudflare Docs:** https://developers.cloudflare.com/pages

---

**Last Updated:** 2026-01-19
