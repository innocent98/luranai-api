# Cloudinary Image Upload Setup

## Overview

The Luran platform uses Cloudinary as the image storage service for tenant widget logos. Cloudinary provides:

- ✅ Automatic image optimization (format, quality, size)
- ✅ CDN delivery for fast global access
- ✅ Image transformations (resize, crop, format conversion)
- ✅ Free tier (25GB storage, 25GB bandwidth/month)

## Quick Setup (5 Minutes)

### 1. Create Cloudinary Account

1. Go to https://cloudinary.com
2. Sign up for a free account
3. Verify your email

### 2. Get Your Credentials

After signup, you'll see your Dashboard:

```
Cloud name: your_cloud_name
API Key: 123456789012345
API Secret: abcdefghijklmnopqrstuvwxyz1234
```

### 3. Update .env Configuration

Add these to your `.env` file:

```bash
# Cloudinary Configuration
CLOUDINARY_CLOUD_NAME=your_cloud_name
CLOUDINARY_API_KEY=123456789012345
CLOUDINARY_API_SECRET=abcdefghijklmnopqrstuvwxyz1234
CLOUDINARY_FOLDER=luran/widget_logos
```

### 4. Install Dependencies

```bash
poetry add cloudinary pillow
# Or if using pip:
# pip install cloudinary pillow
```

### 5. Restart API

```bash
# Restart your FastAPI server
poetry run uvicorn app.main:app --reload
```

### 6. Verify Setup

Check API logs on startup:

```
✅ Cloudinary initialized: your_cloud_name
```

If you see a warning, double-check your credentials in `.env`.

## Using the Image Upload API

### Upload Widget Logo

**Endpoint:** `POST /api/v1/widget/upload-logo`

**Authentication:** Requires tenant admin bearer token

**Request:**
```bash
curl -X POST "http://localhost:8000/api/v1/widget/upload-logo" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -F "file=@/path/to/logo.png"
```

**Response:**
```json
{
  "message": "Logo uploaded successfully",
  "logo_url": "https://res.cloudinary.com/your_cloud_name/image/upload/v1234567890/luran/widget_logos/tenant-uuid_logo.png"
}
```

The logo URL is automatically saved to the tenant's widget configuration.

### Delete Widget Logo

**Endpoint:** `DELETE /api/v1/widget/logo`

**Request:**
```bash
curl -X DELETE "http://localhost:8000/api/v1/widget/logo" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### Update Logo URL Manually (Optional)

You can also set a logo URL directly without uploading:

**Endpoint:** `PATCH /api/v1/widget/config`

**Request:**
```json
{
  "widget_logo_url": "https://example.com/logo.png"
}
```

## Image Requirements

### Supported Formats
- JPEG (.jpg, .jpeg)
- PNG (.png)
- GIF (.gif)
- WebP (.webp)

### Size Limits
- **Max file size:** 5MB
- **Max dimensions:** 4000x4000 pixels
- **Recommended:** 200x200 to 500x500 pixels (square logos work best)

### Automatic Optimizations

Cloudinary automatically applies these transformations:

1. **Resize:** Max 500x500 pixels (maintains aspect ratio)
2. **Quality:** Auto-optimized for web
3. **Format:** Auto-converted to best format (WebP for modern browsers, fallback to JPEG/PNG)
4. **Compression:** Optimized for fast loading

### Example Upload Result

**Original:** logo.png (2048x2048, 3MB)
**Cloudinary URL:** `https://res.cloudinary.com/.../w_500,h_500,c_limit,q_auto:good,f_auto/luran/widget_logos/tenant-uuid_logo.png`
**Delivered:** WebP format, 500x500, ~50KB

## File Organization

### Cloudinary Folder Structure

```
luran/
└── widget_logos/
    ├── tenant-uuid-1_logo.png
    ├── tenant-uuid-2_logo.png
    └── tenant-uuid-3_logo.png
```

Each tenant's logo is named: `{tenant_id}_logo.{ext}`

**Benefits:**
- Uploading a new logo automatically replaces the old one (overwrites)
- No orphaned files
- Easy to locate tenant logos in Cloudinary dashboard

### Tags

Each uploaded image is tagged with:
- `{tenant_id}` - For searching by tenant
- `widget_logo` - For filtering all widget logos

## Cloudinary Dashboard

### View Uploaded Images

1. Log in to https://cloudinary.com/console
2. Click "Media Library" in the sidebar
3. Navigate to `luran/widget_logos` folder
4. See all uploaded tenant logos

### Search Images

**By Tenant:**
```
Tags: tenant-uuid
```

**All Widget Logos:**
```
Tags: widget_logo
```

## Troubleshooting

### "Cloudinary is not configured" Error

**Cause:** Missing or invalid Cloudinary credentials in `.env`

**Fix:**
1. Check `.env` file has all three credentials:
   ```bash
   CLOUDINARY_CLOUD_NAME=...
   CLOUDINARY_API_KEY=...
   CLOUDINARY_API_SECRET=...
   ```
2. Verify credentials match your Cloudinary dashboard
3. Restart the API server

### "Image format not allowed" Error

**Cause:** Uploaded file is not a valid image or unsupported format

**Fix:** Use JPEG, PNG, GIF, or WebP files only

### "File size exceeds maximum" Error

**Cause:** Image file is larger than 5MB

**Fix:** Compress the image before uploading or use a smaller resolution

### Upload Succeeds but Logo Not Showing

**Possible causes:**
1. CORS issue - Cloudinary URL blocked by browser
2. URL not saved to database

**Fix:**
1. Check Cloudinary URL in response is accessible (paste in browser)
2. Verify `widget_logo_url` field updated in database:
   ```sql
   SELECT widget_logo_url FROM tenants WHERE id = 'tenant-uuid';
   ```

## Cost & Limits

### Cloudinary Free Tier

- ✅ 25 GB storage
- ✅ 25 GB bandwidth/month
- ✅ 25,000 transformations/month
- ✅ No credit card required

### Usage Estimates

**Per tenant:**
- Logo size: ~50-200 KB (after optimization)
- Storage: Negligible (200 KB × 100 tenants = 20 MB)
- Bandwidth: ~5-10 MB/month (assuming logo loads 50 times/day)

**Total for 100 tenants:**
- Storage: ~20 MB (0.08% of free tier)
- Bandwidth: ~1 GB/month (4% of free tier)

**Conclusion:** Free tier is more than sufficient for typical usage.

### Upgrading

If you exceed free tier limits:
- Cloudinary Advanced: $89/month (75 GB storage, 75 GB bandwidth)
- Cloudinary Plus: $224/month (150 GB, 150 GB)

## Security

### API Secret Protection

- ✅ API secret is stored in `.env` (never committed to git)
- ✅ Uploads use secure HTTPS
- ✅ Only authenticated tenant admins can upload logos
- ✅ Each tenant can only manage their own logo

### Image Validation

All uploads are validated for:
- ✅ File type (must be valid image)
- ✅ File size (max 5MB)
- ✅ Image dimensions (max 4000×4000)
- ✅ Image integrity (verify image can be opened)

## Alternative: Manual URL Configuration

If you prefer not to use Cloudinary:

1. Host logo on your own CDN/server
2. Use the manual URL update endpoint:
   ```bash
   PATCH /api/v1/widget/config
   Body: { "widget_logo_url": "https://your-cdn.com/logo.png" }
   ```

## Related Files

- `app/utils/cloudinary_client.py` - Cloudinary client implementation
- `app/api/v1/endpoints/widget.py` - Upload/delete endpoints
- `app/schemas/widget.py` - Widget configuration schemas
- `.env` - Cloudinary credentials (dev)
- `.env.prod.example` - Production credentials template
