# Luran Widget - Proof of Concept

**Version:** 1.0.0-poc
**Last Updated:** 2026-01-19
**Status:** Testing Only - Not Production Ready

---

## Overview

This is a minimal proof-of-concept implementation of the Luran widget loader to test backend API integration. It demonstrates:

✅ Fetching tenant configuration from API
✅ Creating a branded floating button
✅ Opening/closing chat iframe
✅ postMessage communication
✅ Dynamic styling based on tenant config

**This is NOT production-ready.** Hand off to frontend team for proper implementation.

---

## Architecture

```
┌─────────────────────────────────────┐
│   Customer's Website (test.html)   │
├─────────────────────────────────────┤
│                                     │
│  widget.js (POC)                    │
│  • Fetches config from API          │
│  • Creates floating button          │
│  • Manages iframe                   │
│                                     │
│  ┌───────────────────────────────┐ │
│  │ Floating Button               │ │
│  │ (styled with tenant colors)   │ │
│  └───────────────────────────────┘ │
│                                     │
│  ┌───────────────────────────────┐ │
│  │ Chat iframe (opens on click)  │ │
│  │ Placeholder for now           │ │
│  └───────────────────────────────┘ │
│                                     │
└─────────────────────────────────────┘
         ↓
    Backend API
    http://localhost:8000/api/v1
```

---

## Setup Instructions

### 1. Prerequisites

- Luran backend API running on `http://localhost:8000`
- Valid Tenant UUID
- Public API Key (starts with `pk_live_...`)

### 2. Configuration

Edit `test.html` lines 550-553:

```javascript
window.luranSettings = {
  tenantId: 'a1b2c3d4-5e6f-7g8h-9i0j-k1l2m3n4o5p6',  // Your Tenant UUID
  apiKey: 'pk_live_abc123def456ghi789',              // Your Public API Key

  // Optional: Override API URLs
  apiUrl: 'http://localhost:8000/api/v1',
  chatUrl: 'http://localhost:3000/widget'
};
```

**How to get these values:**

1. **Tenant UUID:**
   - Login to Luran dashboard
   - Go to Settings → Widget Configuration
   - Copy the Tenant UUID (format: UUID v4)

2. **Public API Key:**
   - Go to Settings → API Keys
   - Create new PUBLIC API key
   - Copy the key (starts with `pk_live_...`)

### 3. Run the Test

**Option A: Open directly in browser**
```bash
open test.html
# or
double-click test.html
```

**Option B: Use local server (recommended)**
```bash
# Python 3
python3 -m http.server 8080

# Node.js (if you have http-server)
npx http-server -p 8080

# Then open: http://localhost:8080/test.html
```

### 4. Verify Setup

Open browser console (F12) and look for:

```
[Luran Widget] Initializing...
[Luran Widget] Tenant ID: a1b2c3d4-...
[Luran Widget] Fetching config from: http://localhost:8000/api/v1/widget/public/config/...
[Luran Widget] Config received: {...}
[Luran Widget] Widget initialized successfully
```

---

## What to Test

### ✅ Visual Tests

1. **Button Appearance**
   - [ ] Widget button appears in configured position (default: bottom-right)
   - [ ] Button uses tenant's primary color
   - [ ] Button has chat icon
   - [ ] Hover effect works (scale + shadow)

2. **Button Interaction**
   - [ ] Click opens iframe
   - [ ] Icon changes to X when open
   - [ ] Click again closes iframe
   - [ ] Icon changes back to chat

3. **Responsive Design**
   - [ ] Resize browser to mobile size (< 500px width)
   - [ ] Iframe should go fullscreen
   - [ ] Button should remain visible

### ✅ Functional Tests

1. **API Integration**
   - [ ] No CORS errors in console
   - [ ] Config fetched successfully (200 OK)
   - [ ] Tenant branding applied (colors, position)
   - [ ] Check console logs for API responses

2. **JavaScript API**
   - [ ] Use test buttons on page
   - [ ] `LuranWidget.open()` works
   - [ ] `LuranWidget.close()` works
   - [ ] `LuranWidget.toggle()` works
   - [ ] `LuranWidget.getConfig()` returns config
   - [ ] `LuranWidget.isOpen()` returns correct state

3. **Error Handling**
   - [ ] Test with invalid API key
   - [ ] Test with invalid tenant ID
   - [ ] Test with API server offline
   - [ ] Widget should show fallback config

---

## API Testing

### Test API Endpoint Directly

```bash
# Replace with your actual tenant UUID and API key
curl -X GET "http://localhost:8000/api/v1/widget/public/config/YOUR_TENANT_UUID" \
  -H "X-API-Key: pk_live_YOUR_API_KEY"
```

**Expected Response:**
```json
{
  "tenant_id": "uuid",
  "company_name": "Your Company",
  "primary_color": "#2563eb",
  "accent_color": "#10b981",
  "logo_url": null,
  "greeting_message": "Hi! How can we help you today?",
  "position": "bottom-right",
  "show_unread_badge": true,
  "allow_file_upload": true
}
```

---

## Troubleshooting

### Widget Not Appearing

**Check 1: Configuration**
```javascript
console.log(window.luranSettings);
// Should show your tenant ID and API key
```

**Check 2: Widget Loaded**
```javascript
console.log(window.LuranWidget);
// Should show object with open, close, toggle methods
```

**Check 3: Console Errors**
- Open DevTools → Console
- Look for red error messages
- Common issues:
  - CORS errors (API needs proper CORS headers)
  - 401 Unauthorized (invalid API key)
  - 404 Not Found (invalid tenant ID)

### Button Appears But Wrong Color

Check tenant configuration:
```javascript
window.LuranWidget.getConfig();
```

Update via API or dashboard:
- Go to Settings → Widget Configuration
- Update Primary Color
- Refresh page

### CORS Errors

Add to backend API (FastAPI):
```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For testing only
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

---

## Files

```
widget-poc/
├── widget.js       # Widget loader (vanilla JS)
├── test.html       # Test page with controls
└── README.md       # This file
```

---

## Public API Methods

The widget exposes a global `LuranWidget` object:

```javascript
// Open chat window
LuranWidget.open();

// Close chat window
LuranWidget.close();

// Toggle chat window
LuranWidget.toggle();

// Get current configuration
const config = LuranWidget.getConfig();
console.log(config.company_name);

// Check if chat is open
const isOpen = LuranWidget.isOpen();  // true or false

// Get version
console.log(LuranWidget.version);  // "1.0.0-poc"
```

---

## Known Limitations (POC)

This is a proof-of-concept with the following limitations:

❌ **Not production-ready**
- No proper error handling
- No retry logic
- No bundle optimization
- No browser compatibility polyfills
- No comprehensive testing

❌ **Missing features**
- No unread badge functionality (API exists, just not wired)
- No session persistence (localStorage not implemented)
- No analytics tracking
- No accessibility features
- No keyboard navigation

❌ **Chat UI**
- iframe loads but shows placeholder
- Real chat UI needs to be built separately (React/Next.js)
- No WebSocket integration yet

---

## Next Steps (For Frontend Team)

### Phase 1: Production Widget.js

1. **Setup proper build pipeline**
   - Webpack/Rollup with Terser minification
   - Target bundle size: < 15KB gzipped
   - Source maps for debugging

2. **Add comprehensive error handling**
   - API timeout handling
   - Retry logic with exponential backoff
   - Fallback configuration
   - User-friendly error messages

3. **Browser compatibility**
   - Add polyfills (fetch, Promise, etc.)
   - Test on IE11 (if required)
   - Test on mobile browsers

4. **Complete features**
   - Session persistence (localStorage)
   - Unread badge updates
   - Analytics integration
   - Custom events for tracking

5. **Testing**
   - Unit tests (Jest)
   - Integration tests
   - E2E tests (Playwright)
   - Browser compatibility tests

### Phase 2: Chat UI Application

1. **Setup Next.js project**
   ```bash
   npx create-next-app@latest luran-chat-ui --typescript
   ```

2. **Core features**
   - Message list with virtualization
   - Message input with file upload
   - WebSocket connection
   - Typing indicators
   - Agent presence
   - Session management

3. **Styling**
   - Tenant branding (colors, logo)
   - Responsive design
   - Dark mode support
   - Animations

4. **Integration**
   - postMessage communication with parent
   - Auth with API
   - WebSocket to backend
   - File upload to backend

---

## Documentation References

- **Full Implementation Guide:** `../docs/WIDGET_INTEGRATION_GUIDE.md`
- **API Documentation:** `../docs/FRONTEND_API_DOCUMENTATION.md`
- **Backend Endpoints:** All widget endpoints are live and working

---

## Testing Checklist

Before handing off to frontend team:

- [ ] Backend API is accessible
- [ ] Widget loads without errors
- [ ] Button appears with correct styling
- [ ] Button position matches config
- [ ] Click opens/closes iframe
- [ ] Console shows no CORS errors
- [ ] API returns correct configuration
- [ ] Public API methods work (`open()`, `close()`, etc.)
- [ ] Mobile responsive works
- [ ] No conflicts with page styles
- [ ] No JavaScript errors in console

---

## Production Deployment (Future)

### Widget.js
```
CDN: https://cdn.luranai.com/widget/v1/widget.js
```

### Chat UI
```
Domain: https://chat.luranai.com/widget
```

### API
```
Domain: https://api.luranai.com/api/v1
```

---

## Support

**Questions about POC:** Check console logs first
**Questions about backend API:** See API documentation
**Frontend implementation:** Hand off to frontend team with this POC + docs

---

**Last Updated:** 2026-01-19
**Created by:** Claude (AI Assistant)
**Purpose:** Backend API testing only
**Status:** ✅ POC Complete - Ready for Frontend Team
