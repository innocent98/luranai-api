# Quick Start - Deploy Widget CDN in 5 Minutes

The fastest way to deploy Luran widget CDN to production.

## Prerequisites

- Vercel account (free): https://vercel.com/signup
- Git installed
- 5 minutes

## Steps

### 1. Install Vercel CLI (30 seconds)

```bash
npm install -g vercel
# or
brew install vercel-cli
```

### 2. Deploy to Production (1 minute)

```bash
# Navigate to cdn-widget directory
cd /path/to/luran/cdn-widget

# Login to Vercel
vercel login

# Deploy
vercel --prod
```

Follow prompts:
- Project name: `luran-widget-cdn`
- Directory: `./`
- Override settings: `No`

**Done!** Note the URL (e.g., `luran-widget-cdn.vercel.app`)

### 3. Add Custom Domain (2 minutes)

```bash
# Add domain via CLI
vercel domains add cdn.luranai.com
```

**Or via Dashboard:**
1. Go to https://vercel.com/dashboard
2. Click your project → Settings → Domains
3. Add: `cdn.luranai.com`

### 4. Configure DNS (2 minutes)

**At your DNS provider (Namecheap, GoDaddy, Cloudflare, etc.):**

```
Type: CNAME
Name: cdn
Value: cname.vercel-dns.com
TTL: 3600
```

**Wait 5-30 minutes for DNS to propagate.**

### 5. Test (30 seconds)

```bash
# Test widget loading
curl https://cdn.luranai.com/widget/v1/widget.js

# Should return JavaScript code
```

## ✅ You're Done!

Your widget is now live at:
```
https://cdn.luranai.com/widget/v1/widget.js
```

## Next Steps

### Enable Auto-Deploy from Git

```bash
# Initialize git (if not already)
git init
git add .
git commit -m "Initial CDN setup"

# Create GitHub repo, then:
git remote add origin https://github.com/yourusername/luran-widget-cdn.git
git push -u origin main

# Link to Vercel
vercel git connect
```

Now every push to `main` auto-deploys! 🚀

### Update Widget

```bash
# Edit widget
vim public/widget/v1/widget.js

# Push changes
git add .
git commit -m "Update widget"
git push

# Vercel auto-deploys in ~30 seconds
```

### Create Version 2

```bash
# Create v2 directory
mkdir -p public/widget/v2

# Copy v1 as starting point
cp public/widget/v1/widget.js public/widget/v2/widget.js

# Edit v2 with new features
vim public/widget/v2/widget.js

# Deploy
git add .
git commit -m "Add widget v2"
git push
```

Access at: `https://cdn.luranai.com/widget/v2/widget.js`

## Troubleshooting

### Widget not loading?

```bash
# Check deployment status
vercel ls

# Check DNS
nslookup cdn.luranai.com

# Check CORS headers
curl -I https://cdn.luranai.com/widget/v1/widget.js | grep -i cors
# Should see: access-control-allow-origin: *
```

### Need help?

See full guide: `DEPLOYMENT_GUIDE.md`

---

**That's it!** You now have a production-ready CDN with:
- ✅ Global edge network (190+ locations)
- ✅ Automatic SSL
- ✅ Unlimited bandwidth (free tier)
- ✅ Auto-deploy from Git
- ✅ CORS configured
- ✅ 1-year cache

**Total time:** < 5 minutes
**Total cost:** $0 (free tier)
