# Luran Widget CDN

CDN hosting for the Luran chat widget loader JavaScript file.

## 🌐 Production URL

```
https://cdn.luranai.com/widget/v1/widget.js
```

## 📁 Directory Structure

```
cdn-widget/
├── public/
│   ├── index.html               # Landing page
│   └── widget/
│       ├── v1/
│       │   └── widget.js        # Version 1 of widget (current)
│       └── v2/                  # Future versions
├── vercel.json                  # Vercel configuration
├── package.json                 # Project metadata
├── .gitignore                   # Git ignore rules
└── README.md                    # This file
```

## 🚀 Deployment

### Prerequisites

1. Install Vercel CLI:
```bash
npm install -g vercel
```

2. Login to Vercel:
```bash
vercel login
```

### Deploy to Production

```bash
# From this directory
cd cdn-widget

# Deploy to production
vercel --prod

# OR use npm script
npm run deploy
```

### Deploy Preview (Staging)

```bash
# Deploy to preview environment
vercel

# OR use npm script
npm run deploy:preview
```

## 🔧 Configuration

### Custom Domain Setup

1. Deploy the project first
2. Go to Vercel dashboard → Project → Settings → Domains
3. Add custom domain: `cdn.luranai.com`
4. Update DNS records at your registrar:
   ```
   Type: CNAME
   Name: cdn
   Value: cname.vercel-dns.com
   ```
5. Wait for DNS propagation (5-60 minutes)

### Environment Variables

No environment variables needed for this static CDN.

## 📦 Adding New Widget Versions

### Create Version 2

```bash
# Create new version directory
mkdir -p public/widget/v2

# Copy and modify widget.js
cp public/widget/v1/widget.js public/widget/v2/widget.js

# Edit widget.js with new features
# ...

# Commit and deploy
git add public/widget/v2/
git commit -m "Add widget v2"
vercel --prod
```

### Update "latest" Alias

In `vercel.json`, the `/widget/latest/widget.js` rewrite points to the current version:

```json
{
  "rewrites": [
    {
      "source": "/widget/latest/widget.js",
      "destination": "/widget/v2/widget.js"  // Update this
    }
  ]
}
```

## 🔒 Security Headers

Configured in `vercel.json`:

- **CORS:** `Access-Control-Allow-Origin: *` (required for widget to load on any domain)
- **Cache:** `Cache-Control: public, max-age=31536000, immutable` (1 year cache)
- **Content Type:** `application/javascript; charset=utf-8`
- **Security:** `X-Content-Type-Options: nosniff`

## 🧪 Testing Locally

```bash
# Start local server
npm run dev

# Test widget loading
curl http://localhost:8080/widget/v1/widget.js

# Open in browser
open http://localhost:8080
```

## 📊 Usage Examples

### Basic Integration

```html
<script>
  window.luranSettings = {
    tenantId: 'your-tenant-uuid',
    apiKey: 'pk_live_...'
  };
</script>
<script src="https://cdn.luranai.com/widget/v1/widget.js"></script>
```

### With Custom Configuration

```html
<script>
  window.luranSettings = {
    tenantId: 'your-tenant-uuid',
    apiKey: 'pk_live_...',
    apiUrl: 'https://api.luranai.com/api/v1',
    chatUrl: 'https://chat.luranai.com/widget'
  };
</script>
<script src="https://cdn.luranai.com/widget/v1/widget.js"></script>
```

### Using "latest" Version

```html
<!-- Always loads the latest stable version -->
<script src="https://cdn.luranai.com/widget/latest/widget.js"></script>
```

## 🔄 Version Management

### Versioning Strategy

- **v1, v2, v3...** - Major versions (breaking changes)
- `/widget/latest/widget.js` - Alias to current stable version
- Old versions remain available for backwards compatibility

### When to Create New Version

- Breaking API changes
- Significant feature rewrites
- Different initialization signatures

### Deprecation Policy

1. Announce deprecation 6 months in advance
2. Keep deprecated versions live for 12 months
3. Provide migration guide
4. Show console warnings in deprecated versions

## 🌍 CDN Performance

### Vercel Edge Network

- **190+ edge locations worldwide**
- **Automatic caching** at edge
- **< 100ms** response time globally
- **Unlimited bandwidth** on Pro plan

### Cache Behavior

- **widget.js:** Cached for 1 year (immutable)
- **New versions:** Instant propagation to all edge nodes
- **Purge cache:** Automatic on new deployment

## 📈 Monitoring

### Check Widget Health

```bash
# Test from different regions
curl -I https://cdn.luranai.com/widget/v1/widget.js

# Expected response:
# HTTP/2 200
# content-type: application/javascript; charset=utf-8
# cache-control: public, max-age=31536000, immutable
# access-control-allow-origin: *
```

### Vercel Analytics

1. Go to Vercel dashboard → Project → Analytics
2. View:
   - Requests per second
   - Response times
   - Cache hit rate
   - Bandwidth usage
   - Top countries

## 🛠️ Troubleshooting

### Widget Not Loading

**Check 1: Network Tab**
- Open browser DevTools → Network
- Look for widget.js request
- Verify status is 200 OK

**Check 2: CORS Headers**
```bash
curl -I https://cdn.luranai.com/widget/v1/widget.js | grep -i cors
# Should see: access-control-allow-origin: *
```

**Check 3: Content Type**
```bash
curl -I https://cdn.luranai.com/widget/v1/widget.js | grep -i content-type
# Should see: content-type: application/javascript
```

### Cache Issues

**Force Cache Refresh:**
```bash
# Deploy new version
vercel --prod --force

# Or purge via Vercel dashboard
# Project → Deployments → ... → Purge Cache
```

### DNS Not Resolving

**Check DNS Propagation:**
```bash
# Check DNS
dig cdn.luranai.com

# Should see CNAME to Vercel
nslookup cdn.luranai.com
```

## 🔐 Security Considerations

### What's Safe

✅ Widget.js is public (safe to expose)
✅ CORS allows all origins (required)
✅ No secrets in widget.js
✅ API keys are PUBLIC keys only (pk_live_*)

### What to Protect

❌ Never put SECRET keys (sk_live_*) in widget.js
❌ Don't include tenant-specific data in CDN files
❌ No user data in client-side code

## 📝 Maintenance

### Regular Tasks

**Monthly:**
- Review Vercel analytics
- Check bandwidth usage
- Monitor error rates

**Quarterly:**
- Review widget versions
- Plan deprecations
- Update documentation

**Yearly:**
- Audit security headers
- Review caching strategy
- Plan major version updates

## 🆘 Support

- **Documentation:** https://docs.luranai.com
- **Status Page:** https://status.luranai.com
- **Email:** support@luranai.com

---

**Last Updated:** 2026-01-19
**Maintained by:** Luran Platform Team
