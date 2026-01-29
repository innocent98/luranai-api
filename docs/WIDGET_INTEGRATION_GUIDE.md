# Widget Integration Guide - Embeddable Chat Widget

**Last Updated:** 2026-01-15
**Widget Version:** 1.0
**CDN URL:** `https://cdn.luranai.com/widget/v1/widget.js`

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Quick Start](#quick-start)
3. [Widget SDK Implementation](#widget-sdk-implementation)
4. [Floating Button Component](#floating-button-component)
5. [Chat Window (iframe)](#chat-window-iframe)
6. [API Integration](#api-integration)
7. [Styling and Customization](#styling-and-customization)
8. [Build and Deployment](#build-and-deployment)
9. [Browser Compatibility](#browser-compatibility)
10. [Testing Guide](#testing-guide)

---

## Architecture Overview

### Three-Layer Architecture

```
┌─────────────────────────────────────────────────┐
│         Customer's Website (example.com)        │
├─────────────────────────────────────────────────┤
│                                                 │
│  ┌───────────────────────────────────────────┐ │
│  │ Widget Loader (widget.js)                 │ │
│  │ • Loads configuration from API            │ │
│  │ • Creates floating button                 │ │
│  │ • Manages iframe lifecycle                │ │
│  └─────────────────┬─────────────────────────┘ │
│                    │                            │
│                    ▼                            │
│  ┌───────────────────────────────────────────┐ │
│  │ Floating Button                           │ │
│  │ • Branded with tenant colors              │ │
│  │ • Shows unread count badge                │ │
│  │ • Click to expand chat                    │ │
│  └─────────────────┬─────────────────────────┘ │
│                    │                            │
│                    ▼                            │
│  ┌───────────────────────────────────────────┐ │
│  │ Chat Window (iframe)                      │ │
│  │ • Isolated environment                    │ │
│  │ • Loads from chat.luranai.com               │ │
│  │ • Full chat UI                            │ │
│  └───────────────────────────────────────────┘ │
│                                                 │
└─────────────────────────────────────────────────┘

External Services:
- API: https://api.luranai.com/api/v1
- Chat UI: https://chat.luranai.com/widget
- CDN: https://cdn.luranai.com/widget/v1
```

### Key Components

**Important:** The widget system consists of two separate components:

1. **Widget Loader** (`widget.js`) - **Runs on customer's website**
   - Lightweight JavaScript file (~10KB gzipped)
   - Loads asynchronously, doesn't block page load
   - Fetches tenant configuration from API
   - Creates floating button DOM element
   - Manages iframe lifecycle
   - **Does NOT contain chat UI** - only the loader and button

2. **Floating Button** (DOM element)
   - Fixed position on customer's website
   - Styled with tenant's brand colors
   - Shows unread message badge
   - Opens/closes chat window on click

3. **Chat Window** (iframe) - **Separate application**
   - Loads React/Vue app from `chat.luranai.com/widget`
   - Completely isolated from parent page styles
   - Handles all chat interactions (messages, WebSocket, etc.)
   - Communicates with parent via postMessage
   - **This is the actual chat UI application** - separate from widget.js

**Note:** Widget.js and Chat UI are two different things:
- **widget.js** = Doorbell button (small loader script)
- **Chat UI** = Video call system (full chat application in iframe)

---

## Quick Start

### For Customers (Installation)

1. Get your Public API Key from Luran dashboard
2. Add this code before `</body>` tag on your website:

```html
<!-- Luran Widget Integration -->
<script>
  window.luranSettings = {
    tenantId: 'your-tenant-uuid-here',  // Your Tenant UUID from dashboard
    apiKey: 'pk_live_your_public_api_key_here'
  };
</script>
<script src="https://cdn.luranai.com/widget/v1/widget.js" async defer></script>
<!-- End Luran Widget -->
```

**Important:** The `tenantId` must be your **Tenant UUID** (a unique identifier like `a1b2c3d4-5e6f-7g8h-9i0j-k1l2m3n4o5p6`), NOT your company name. You can find your Tenant UUID in the Luran dashboard under Settings → Widget Configuration.

3. That's it! The widget will appear on your site.

---

## Widget SDK Implementation

### Implementation Status

**✅ Backend API - IMPLEMENTED:**
- All widget endpoints exist and working
- Tenant configuration API active
- Public widget config endpoint functional

**📝 Frontend Components - TO BE BUILT:**
This section describes the implementation plan for two frontend components that need to be built:

1. **Widget Loader (widget.js)** - TO BE BUILT
   - Small JavaScript file for customer websites
   - Creates floating button and manages iframe

2. **Chat UI Application** - TO BE BUILT
   - React/Vue application that runs in iframe
   - Full chat interface with WebSocket support

### Project Structure

**For Widget Loader (widget.js):**
```
luran-widget/
├── src/
│   ├── widget-loader.js       # Main entry point
│   ├── floating-button.js     # Floating button component
│   ├── iframe-manager.js      # Manages chat iframe
│   ├── api-client.js          # API integration
│   ├── storage.js             # LocalStorage management
│   ├── styles.js              # Dynamic styles
│   └── utils.js               # Helper functions
├── dist/
│   └── widget.js              # Compiled bundle
├── package.json
├── webpack.config.js
└── README.md
```

**For Chat UI (separate React/Vue app):**
```
luran-chat-ui/
├── src/
│   ├── components/
│   │   ├── ChatWindow.tsx
│   │   ├── MessageList.tsx
│   │   ├── MessageInput.tsx
│   │   └── Header.tsx
│   ├── hooks/
│   │   ├── useWebSocket.ts
│   │   └── useMessages.ts
│   ├── App.tsx
│   └── main.tsx
├── package.json
└── vite.config.ts
```

### Main Entry Point: `widget-loader.js`

```javascript
/**
 * Luran Widget Loader
 *
 * This is the main entry point that loads on customer websites.
 * It fetches configuration, creates the floating button, and manages the chat iframe.
 */

(function() {
  'use strict';

  // Namespace to avoid conflicts
  window.Luran = window.Luran || {};

  // Configuration from customer's website
  const settings = window.luranSettings || {};

  if (!settings.tenantId || !settings.apiKey) {
    console.error('[Luran Widget] Missing tenantId or apiKey in luranSettings');
    return;
  }

  // Constants
  const API_BASE_URL = 'https://api.luranai.com/api/v1';
  const CHAT_UI_URL = 'https://chat.luranai.com/widget';
  const STORAGE_KEY = 'luran_widget_';

  // Widget state
  let config = null;
  let button = null;
  let iframe = null;
  let isOpen = false;
  let unreadCount = 0;

  /**
   * Initialize the widget
   */
  async function init() {
    try {
      // Fetch tenant configuration
      config = await fetchConfig();

      if (!config) {
        console.error('[Luran Widget] Failed to load configuration');
        return;
      }

      // Create floating button
      button = createFloatingButton();
      document.body.appendChild(button);

      // Add button click listener
      button.addEventListener('click', toggleChat);

      // Load previous session if exists
      loadPreviousSession();

      console.log('[Luran Widget] Initialized successfully');
    } catch (error) {
      console.error('[Luran Widget] Initialization error:', error);
    }
  }

  /**
   * Fetch widget configuration from API
   * Note: settings.tenantId should be the Tenant UUID
   */
  async function fetchConfig() {
    try {
      const response = await fetch(
        `${API_BASE_URL}/widget/public/config/${settings.tenantId}`,  // Tenant UUID required
        {
          headers: {
            'X-API-Key': settings.apiKey,
            'Content-Type': 'application/json'
          }
        }
      );

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      return await response.json();
    } catch (error) {
      console.error('[Luran Widget] Failed to fetch config:', error);
      return null;
    }
  }

  /**
   * Create floating button DOM element
   */
  function createFloatingButton() {
    const btn = document.createElement('button');
    btn.id = 'luran-widget-button';
    btn.setAttribute('aria-label', 'Open chat');
    btn.innerHTML = `
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 2C6.48 2 2 6.48 2 12C2 13.93 2.63 15.71 3.69 17.17L2.29 21.71L7.07 20.35C8.47 21.25 10.18 21.75 12 21.75C17.52 21.75 22 17.27 22 11.75C22 6.23 17.52 2 12 2Z" fill="white"/>
      </svg>
      <span class="luran-badge" style="display: none;">0</span>
    `;

    // Apply styles
    applyButtonStyles(btn);

    return btn;
  }

  /**
   * Apply dynamic styles to button based on tenant config
   */
  function applyButtonStyles(btn) {
    const position = config.position || 'bottom-right';
    const primaryColor = config.primary_color || '#2563eb';

    // Base styles
    btn.style.cssText = `
      position: fixed;
      width: 60px;
      height: 60px;
      border-radius: 50%;
      background-color: ${primaryColor};
      color: white;
      border: none;
      cursor: pointer;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
      z-index: 999999;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.3s ease, box-shadow 0.3s ease;
    `;

    // Position
    switch (position) {
      case 'bottom-right':
        btn.style.bottom = '20px';
        btn.style.right = '20px';
        break;
      case 'bottom-left':
        btn.style.bottom = '20px';
        btn.style.left = '20px';
        break;
      case 'top-right':
        btn.style.top = '20px';
        btn.style.right = '20px';
        break;
      case 'top-left':
        btn.style.top = '20px';
        btn.style.left = '20px';
        break;
    }

    // Hover effect
    btn.addEventListener('mouseenter', () => {
      btn.style.transform = 'scale(1.1)';
      btn.style.boxShadow = '0 6px 16px rgba(0, 0, 0, 0.2)';
    });

    btn.addEventListener('mouseleave', () => {
      btn.style.transform = 'scale(1)';
      btn.style.boxShadow = '0 4px 12px rgba(0, 0, 0, 0.15)';
    });
  }

  /**
   * Toggle chat window open/close
   */
  function toggleChat() {
    if (isOpen) {
      closeChat();
    } else {
      openChat();
    }
  }

  /**
   * Open chat window
   */
  function openChat() {
    if (!iframe) {
      iframe = createChatIframe();
      document.body.appendChild(iframe);
    }

    iframe.style.display = 'block';
    isOpen = true;

    // Reset unread count
    if (config.show_unread_badge) {
      updateUnreadBadge(0);
    }

    // Update button icon to close icon
    button.innerHTML = `
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M19 6.41L17.59 5L12 10.59L6.41 5L5 6.41L10.59 12L5 17.59L6.41 19L12 13.41L17.59 19L19 17.59L13.41 12L19 6.41Z" fill="white"/>
      </svg>
    `;
  }

  /**
   * Close chat window
   */
  function closeChat() {
    if (iframe) {
      iframe.style.display = 'none';
    }
    isOpen = false;

    // Update button icon to chat icon
    button.innerHTML = `
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 2C6.48 2 2 6.48 2 12C2 13.93 2.63 15.71 3.69 17.17L2.29 21.71L7.07 20.35C8.47 21.25 10.18 21.75 12 21.75C17.52 21.75 22 17.27 22 11.75C22 6.23 17.52 2 12 2Z" fill="white"/>
      </svg>
      ${config.show_unread_badge ? '<span class="luran-badge" style="display: none;">0</span>' : ''}
    `;
  }

  /**
   * Create chat iframe
   */
  function createChatIframe() {
    const frame = document.createElement('iframe');
    frame.id = 'luran-chat-iframe';
    frame.src = `${CHAT_UI_URL}?tenantId=${settings.tenantId}&apiKey=${settings.apiKey}`;  // tenantId = UUID
    frame.setAttribute('allow', 'clipboard-write');
    frame.setAttribute('title', 'Luran Chat Widget');

    // Apply iframe styles
    applyIframeStyles(frame);

    // Listen for messages from iframe
    window.addEventListener('message', handleIframeMessage);

    return frame;
  }

  /**
   * Apply styles to iframe
   */
  function applyIframeStyles(frame) {
    const position = config.position || 'bottom-right';

    frame.style.cssText = `
      position: fixed;
      border: none;
      width: 400px;
      height: 600px;
      max-width: 100vw;
      max-height: 100vh;
      border-radius: 12px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.15);
      z-index: 999998;
      display: none;
      background: white;
    `;

    // Position relative to button
    switch (position) {
      case 'bottom-right':
        frame.style.bottom = '90px';
        frame.style.right = '20px';
        break;
      case 'bottom-left':
        frame.style.bottom = '90px';
        frame.style.left = '20px';
        break;
      case 'top-right':
        frame.style.top = '90px';
        frame.style.right = '20px';
        break;
      case 'top-left':
        frame.style.top = '90px';
        frame.style.left = '20px';
        break;
    }

    // Mobile responsive
    if (window.innerWidth < 500) {
      frame.style.width = '100vw';
      frame.style.height = '100vh';
      frame.style.bottom = '0';
      frame.style.right = '0';
      frame.style.left = '0';
      frame.style.top = '0';
      frame.style.borderRadius = '0';
    }
  }

  /**
   * Handle messages from chat iframe
   */
  function handleIframeMessage(event) {
    // Verify origin
    if (!event.origin.includes('luran.com')) {
      return;
    }

    const { type, data } = event.data;

    switch (type) {
      case 'CLOSE_CHAT':
        closeChat();
        break;

      case 'UPDATE_UNREAD':
        updateUnreadBadge(data.count);
        break;

      case 'SESSION_STARTED':
        saveSession(data.sessionId);
        break;

      case 'MINIMIZE_CHAT':
        closeChat();
        break;

      default:
        // Unknown message type
        break;
    }
  }

  /**
   * Update unread badge
   */
  function updateUnreadBadge(count) {
    if (!config.show_unread_badge) return;

    unreadCount = count;
    const badge = button.querySelector('.luran-badge');

    if (badge) {
      if (count > 0) {
        badge.textContent = count > 99 ? '99+' : count;
        badge.style.display = 'flex';
      } else {
        badge.style.display = 'none';
      }
    }
  }

  /**
   * Save session to localStorage
   */
  function saveSession(sessionId) {
    try {
      localStorage.setItem(`${STORAGE_KEY}session`, sessionId);
    } catch (error) {
      console.error('[Luran Widget] Failed to save session:', error);
    }
  }

  /**
   * Load previous session
   */
  function loadPreviousSession() {
    try {
      const sessionId = localStorage.getItem(`${STORAGE_KEY}session`);
      if (sessionId) {
        // Session exists, can auto-open or show badge
        console.log('[Luran Widget] Previous session found:', sessionId);
      }
    } catch (error) {
      console.error('[Luran Widget] Failed to load session:', error);
    }
  }

  /**
   * Public API
   */
  window.Luran = {
    open: openChat,
    close: closeChat,
    toggle: toggleChat,
    getConfig: () => config,
    isOpen: () => isOpen
  };

  // Initialize when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
```

---

## Floating Button Component

### Button Styles

```css
/* Badge styles */
.luran-badge {
  position: absolute;
  top: -5px;
  right: -5px;
  background-color: #ef4444;
  color: white;
  border-radius: 12px;
  padding: 2px 6px;
  font-size: 12px;
  font-weight: bold;
  min-width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
}

/* Button animation */
@keyframes pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(37, 99, 235, 0.7);
  }
  70% {
    box-shadow: 0 0 0 10px rgba(37, 99, 235, 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(37, 99, 235, 0);
  }
}

#luran-widget-button.pulse {
  animation: pulse 2s infinite;
}
```

### Button Positioning

The button position is configured per tenant:
- `bottom-right` (default)
- `bottom-left`
- `top-right`
- `top-left`

### Mobile Responsiveness

```javascript
// Adjust button size on mobile
function adjustForMobile() {
  if (window.innerWidth < 768) {
    button.style.width = '50px';
    button.style.height = '50px';
    button.style.bottom = '15px';
    button.style.right = '15px';
  }
}

window.addEventListener('resize', adjustForMobile);
```

---

## Chat Window (iframe)

### Why iframe?

1. **Style Isolation**: Parent website styles don't affect chat UI
2. **Script Isolation**: No conflicts with parent JavaScript
3. **Security**: Sandboxed environment
4. **Easy Updates**: Chat UI can be updated independently

### iframe URL Structure

```
https://chat.luranai.com/widget?tenantId={tenant-uuid}&apiKey=pk_live_...&sessionId=optional
```

**Example:**
```
https://chat.luranai.com/widget?tenantId=a1b2c3d4-5e6f-7g8h-9i0j-k1l2m3n4o5p6&apiKey=pk_live_abc123&sessionId=optional
```

### iframe Communication (postMessage)

**Parent → iframe:**

```javascript
// Parent sends message to iframe
iframe.contentWindow.postMessage({
  type: 'INIT',
  data: {
    config: config,
    sessionId: previousSessionId
  }
}, 'https://chat.luranai.com');
```

**iframe → Parent:**

```javascript
// iframe sends message to parent
window.parent.postMessage({
  type: 'UPDATE_UNREAD',
  data: {
    count: 3
  }
}, '*');
```

### Message Types

**From Parent to iframe:**
- `INIT` - Initialize with config
- `CLOSE` - Close chat window
- `OPEN` - Open chat window

**From iframe to Parent:**
- `CLOSE_CHAT` - Request to close chat
- `MINIMIZE_CHAT` - Request to minimize chat
- `UPDATE_UNREAD` - Update unread message count
- `SESSION_STARTED` - New session created
- `RESIZE` - Request resize (future)

---

## API Integration

### Fetch Configuration

**Endpoint:** `GET /widget/public/config/{tenant_id}`

**Important:** The `tenant_id` parameter must be your **Tenant UUID** (not company name).

```javascript
async function fetchConfig() {
  const response = await fetch(
    `https://api.luranai.com/api/v1/widget/public/config/${tenantId}`,  // tenantId = UUID
    {
      headers: {
        'X-API-Key': apiKey,
        'Content-Type': 'application/json'
      }
    }
  );

  if (!response.ok) {
    throw new Error(`Failed to load config: ${response.status}`);
  }

  return await response.json();
}
```

**Example Request:**
```
GET https://api.luranai.com/api/v1/widget/public/config/a1b2c3d4-5e6f-7g8h-9i0j-k1l2m3n4o5p6
X-API-Key: pk_live_abc123def456
```

**Response:**
```json
{
  "tenant_id": "uuid",
  "company_name": "Acme Corporation",
  "primary_color": "#2563eb",
  "accent_color": "#10b981",
  "logo_url": "https://acmecorp.com/logo.png",
  "greeting_message": "Hi! How can we help you today?",
  "position": "bottom-right",
  "show_unread_badge": true,
  "allow_file_upload": true
}
```

### Error Handling

```javascript
async function fetchConfigWithRetry(maxRetries = 3) {
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await fetchConfig();
    } catch (error) {
      console.error(`[Luran Widget] Fetch attempt ${i + 1} failed:`, error);

      if (i === maxRetries - 1) {
        // Show fallback button
        return createFallbackConfig();
      }

      // Wait before retry
      await new Promise(resolve => setTimeout(resolve, 1000 * (i + 1)));
    }
  }
}

function createFallbackConfig() {
  return {
    company_name: 'Support',
    primary_color: '#2563eb',
    accent_color: '#10b981',
    logo_url: null,
    greeting_message: 'Hi! How can we help you today?',
    position: 'bottom-right',
    show_unread_badge: true,
    allow_file_upload: true
  };
}
```

---

## Styling and Customization

### Dynamic Theme Application

```javascript
function applyTheme(config) {
  // Create CSS variables for easy theming
  const style = document.createElement('style');
  style.id = 'luran-widget-theme';
  style.textContent = `
    :root {
      --luran-primary: ${config.primary_color};
      --luran-accent: ${config.accent_color};
    }
  `;
  document.head.appendChild(style);
}
```

### Responsive Design

```css
/* Desktop */
@media (min-width: 768px) {
  #luran-chat-iframe {
    width: 400px;
    height: 600px;
    border-radius: 12px;
  }
}

/* Tablet */
@media (min-width: 481px) and (max-width: 767px) {
  #luran-chat-iframe {
    width: 90vw;
    height: 70vh;
    border-radius: 12px;
  }
}

/* Mobile */
@media (max-width: 480px) {
  #luran-chat-iframe {
    width: 100vw;
    height: 100vh;
    border-radius: 0;
    top: 0 !important;
    right: 0 !important;
    bottom: 0 !important;
    left: 0 !important;
  }

  #luran-widget-button {
    width: 50px;
    height: 50px;
  }
}
```

---

## Build and Deployment

### Webpack Configuration

```javascript
// webpack.config.js
const path = require('path');
const TerserPlugin = require('terser-webpack-plugin');

module.exports = {
  entry: './src/widget-loader.js',
  output: {
    filename: 'widget.js',
    path: path.resolve(__dirname, 'dist'),
    library: 'Luran',
    libraryTarget: 'umd',
    globalObject: 'this'
  },
  mode: 'production',
  optimization: {
    minimize: true,
    minimizer: [
      new TerserPlugin({
        terserOptions: {
          compress: {
            drop_console: true,
            drop_debugger: true
          },
          output: {
            comments: false
          }
        },
        extractComments: false
      })
    ]
  },
  module: {
    rules: [
      {
        test: /\.js$/,
        exclude: /node_modules/,
        use: {
          loader: 'babel-loader',
          options: {
            presets: ['@babel/preset-env']
          }
        }
      }
    ]
  }
};
```

### Build Commands

```json
// package.json
{
  "name": "luran-widget",
  "version": "1.0.0",
  "scripts": {
    "build": "webpack --mode production",
    "build:dev": "webpack --mode development --watch",
    "serve": "webpack serve --mode development",
    "analyze": "webpack-bundle-analyzer dist/stats.json"
  },
  "devDependencies": {
    "@babel/core": "^7.23.0",
    "@babel/preset-env": "^7.23.0",
    "babel-loader": "^9.1.3",
    "terser-webpack-plugin": "^5.3.9",
    "webpack": "^5.89.0",
    "webpack-cli": "^5.1.4",
    "webpack-dev-server": "^4.15.1"
  }
}
```

### CDN Deployment

1. Build production bundle:
   ```bash
   npm run build
   ```

2. Upload to CDN (AWS S3, Cloudflare, etc.):
   ```bash
   aws s3 cp dist/widget.js s3://cdn.luranai.com/widget/v1/widget.js \
     --acl public-read \
     --cache-control "max-age=31536000, immutable"
   ```

3. Create versioned URLs:
   - `https://cdn.luranai.com/widget/v1/widget.js` (auto-updates)
   - `https://cdn.luranai.com/widget/v1.0.0/widget.js` (pinned version)

### Bundle Size Optimization

**Target Size:** < 15KB gzipped

**Optimization Strategies:**
1. Tree shaking (remove unused code)
2. Minification (Terser)
3. Compression (gzip/brotli)
4. Lazy loading (load iframe only when opened)
5. No external dependencies

**Check bundle size:**
```bash
npm run build
gzip -c dist/widget.js | wc -c
```

---

## Browser Compatibility

### Supported Browsers

- Chrome/Edge: Last 2 versions
- Firefox: Last 2 versions
- Safari: Last 2 versions
- Mobile Safari (iOS): 12+
- Chrome Android: Last 2 versions

### Polyfills

```javascript
// Check for required features
if (!window.fetch || !window.Promise || !window.localStorage) {
  console.error('[Luran Widget] Browser not supported. Please upgrade.');
  return;
}

// Optional: Load polyfills dynamically
function loadPolyfills() {
  if (!window.fetch) {
    return loadScript('https://cdn.jsdelivr.net/npm/whatwg-fetch@3.6.2/dist/fetch.umd.js');
  }
  return Promise.resolve();
}
```

### Feature Detection

```javascript
const features = {
  localStorage: () => {
    try {
      const test = '__test__';
      localStorage.setItem(test, test);
      localStorage.removeItem(test);
      return true;
    } catch (e) {
      return false;
    }
  },

  postMessage: () => {
    return typeof window.postMessage === 'function';
  },

  iframe: () => {
    return typeof document.createElement('iframe').sandbox !== 'undefined';
  }
};

// Check all required features
function checkCompatibility() {
  const required = ['localStorage', 'postMessage', 'iframe'];
  const unsupported = required.filter(feature => !features[feature]());

  if (unsupported.length > 0) {
    console.error(`[Luran Widget] Unsupported features: ${unsupported.join(', ')}`);
    return false;
  }

  return true;
}
```

---

## Testing Guide

### Unit Testing

```javascript
// tests/widget-loader.test.js
import { fetchConfig, createFloatingButton } from '../src/widget-loader';

describe('Widget Loader', () => {
  test('fetchConfig should return valid config', async () => {
    const config = await fetchConfig('test_tenant', 'pk_live_test');
    expect(config).toHaveProperty('company_name');
    expect(config).toHaveProperty('primary_color');
  });

  test('createFloatingButton should create button element', () => {
    const button = createFloatingButton({
      primary_color: '#2563eb',
      position: 'bottom-right'
    });
    expect(button.tagName).toBe('BUTTON');
    expect(button.id).toBe('luran-widget-button');
  });
});
```

### Integration Testing

```javascript
// tests/integration.test.js
describe('Widget Integration', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    window.luranSettings = {
      tenantId: 'test_tenant',
      apiKey: 'pk_live_test'
    };
  });

  test('should initialize widget on page load', async () => {
    await loadWidget();

    const button = document.getElementById('luran-widget-button');
    expect(button).toBeTruthy();
  });

  test('should open chat when button clicked', async () => {
    await loadWidget();

    const button = document.getElementById('luran-widget-button');
    button.click();

    const iframe = document.getElementById('luran-chat-iframe');
    expect(iframe.style.display).toBe('block');
  });
});
```

### E2E Testing (Playwright)

```javascript
// tests/e2e/widget.spec.js
const { test, expect } = require('@playwright/test');

test.describe('Widget E2E', () => {
  test('should load widget on customer website', async ({ page }) => {
    await page.goto('https://example.com');

    // Widget should be visible
    const button = await page.locator('#luran-widget-button');
    await expect(button).toBeVisible();
  });

  test('should open chat when button clicked', async ({ page }) => {
    await page.goto('https://example.com');

    // Click button
    await page.click('#luran-widget-button');

    // Chat iframe should be visible
    const iframe = await page.locator('#luran-chat-iframe');
    await expect(iframe).toBeVisible();
  });

  test('should load with correct tenant branding', async ({ page }) => {
    await page.goto('https://example.com');

    const button = await page.locator('#luran-widget-button');
    const bgColor = await button.evaluate(el =>
      window.getComputedStyle(el).backgroundColor
    );

    expect(bgColor).toBe('rgb(37, 99, 235)'); // #2563eb
  });
});
```

### Manual Testing Checklist

- [ ] Widget loads without errors
- [ ] Button appears in correct position
- [ ] Button has correct colors from tenant config
- [ ] Logo displays correctly (if provided)
- [ ] Button click opens chat iframe
- [ ] Chat iframe loads correctly
- [ ] Close button works
- [ ] Unread badge displays correctly
- [ ] Mobile responsive (test on phone)
- [ ] Works on different browsers
- [ ] Doesn't conflict with parent page styles
- [ ] Performance is good (< 100ms load time)
- [ ] No console errors

---

## Advanced Features

### Custom Events

```javascript
// Dispatch custom events for analytics
function dispatchWidgetEvent(eventName, data) {
  window.dispatchEvent(new CustomEvent('luran:' + eventName, {
    detail: data
  }));
}

// Usage (tenantId = UUID)
dispatchWidgetEvent('chat_opened', { tenantId: 'a1b2c3d4-5e6f-7g8h-9i0j-k1l2m3n4o5p6' });
dispatchWidgetEvent('message_sent', { sessionId: 'abc123' });

// Listen for events
window.addEventListener('luran:chat_opened', (event) => {
  console.log('Chat opened:', event.detail);
  // Send to analytics
});
```

### Analytics Integration

```javascript
// Google Analytics example
function trackWidgetEvent(action, label) {
  if (window.gtag) {
    gtag('event', action, {
      event_category: 'Luran Widget',
      event_label: label
    });
  }
}

// Track events
trackWidgetEvent('widget_loaded', config.company_name);
trackWidgetEvent('chat_opened', config.company_name);
trackWidgetEvent('message_sent', config.company_name);
```

### Localization

```javascript
// Language detection and loading
const userLanguage = navigator.language || 'en';

const translations = {
  en: {
    open_chat: 'Open chat',
    close_chat: 'Close chat',
    minimize: 'Minimize'
  },
  es: {
    open_chat: 'Abrir chat',
    close_chat: 'Cerrar chat',
    minimize: 'Minimizar'
  },
  fr: {
    open_chat: 'Ouvrir le chat',
    close_chat: 'Fermer le chat',
    minimize: 'Réduire'
  }
};

function t(key) {
  const lang = userLanguage.split('-')[0];
  return translations[lang]?.[key] || translations.en[key];
}
```

### Accessibility (a11y)

```javascript
// Add ARIA attributes
button.setAttribute('aria-label', t('open_chat'));
button.setAttribute('role', 'button');
button.setAttribute('tabindex', '0');

// Keyboard support
button.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    toggleChat();
  }
});

// Screen reader announcements
function announce(message) {
  const liveRegion = document.getElementById('luran-live-region');
  if (!liveRegion) {
    const region = document.createElement('div');
    region.id = 'luran-live-region';
    region.setAttribute('role', 'status');
    region.setAttribute('aria-live', 'polite');
    region.style.cssText = 'position: absolute; left: -10000px; width: 1px; height: 1px; overflow: hidden;';
    document.body.appendChild(region);
  }
  document.getElementById('luran-live-region').textContent = message;
}

// Usage
announce(t('chat_opened'));
```

---

## Troubleshooting

### Widget Not Loading

**Check:**
1. `luranSettings` object is defined
2. `tenantId` and `apiKey` are correct
3. Script loads successfully (check Network tab)
4. No JavaScript errors in console
5. API endpoint is reachable

**Debug:**
```javascript
console.log('Luran Settings:', window.luranSettings);
console.log('Widget Loaded:', window.Luran !== undefined);
```

### Button Not Appearing

**Check:**
1. CSS z-index conflicts with parent page
2. Parent page has `overflow: hidden` on body
3. Button is being created (check DOM)
4. Button styles are applied correctly

**Debug:**
```javascript
const button = document.getElementById('luran-widget-button');
console.log('Button exists:', button !== null);
console.log('Button styles:', button ? button.style.cssText : 'N/A');
```

### iframe Not Loading

**Check:**
1. CORS issues (check Network tab)
2. CSP (Content Security Policy) restrictions
3. iframe sandbox attributes
4. X-Frame-Options header

**Debug:**
```javascript
const iframe = document.getElementById('luran-chat-iframe');
console.log('iframe exists:', iframe !== null);
console.log('iframe src:', iframe?.src);
```

### API Errors

**Check:**
1. API key is valid and active
2. Tenant name is correct
3. Network connectivity
4. API endpoint status

**Debug:**
```javascript
// Test API directly (replace with your actual Tenant UUID)
fetch('https://api.luranai.com/api/v1/widget/public/config/a1b2c3d4-5e6f-7g8h-9i0j-k1l2m3n4o5p6', {
  headers: {
    'X-API-Key': 'pk_live_test'
  }
})
.then(r => r.json())
.then(data => console.log('API Response:', data))
.catch(err => console.error('API Error:', err));
```

**Note:** Use your actual Tenant UUID (not company name) in the URL path.

---

## Performance Optimization

### Lazy Loading

```javascript
// Don't create iframe until user clicks button
let iframeLoaded = false;

function openChat() {
  if (!iframeLoaded) {
    iframe = createChatIframe();
    document.body.appendChild(iframe);
    iframeLoaded = true;
  }

  iframe.style.display = 'block';
  isOpen = true;
}
```

### Resource Hints

```html
<!-- Add to customer installation code -->
<link rel="dns-prefetch" href="https://api.luranai.com">
<link rel="preconnect" href="https://chat.luranai.com">
```

### Caching Strategy

```javascript
// Cache config in localStorage
const CACHE_DURATION = 30 * 60 * 1000; // 30 minutes

function getCachedConfig() {
  try {
    const cached = localStorage.getItem(`${STORAGE_KEY}config`);
    if (cached) {
      const { config, timestamp } = JSON.parse(cached);
      if (Date.now() - timestamp < CACHE_DURATION) {
        return config;
      }
    }
  } catch (error) {
    console.error('[Luran Widget] Cache error:', error);
  }
  return null;
}

function setCachedConfig(config) {
  try {
    localStorage.setItem(`${STORAGE_KEY}config`, JSON.stringify({
      config,
      timestamp: Date.now()
    }));
  } catch (error) {
    console.error('[Luran Widget] Cache error:', error);
  }
}
```

---

## Security Best Practices

### 1. API Key Protection

```javascript
// PUBLIC keys are safe to expose
// SECRET keys should NEVER be in widget code

if (settings.apiKey.startsWith('sk_')) {
  console.error('[Luran Widget] SECRET API keys should not be used in widget. Use PUBLIC keys only.');
  return;
}
```

### 2. XSS Prevention

```javascript
// Sanitize user input before displaying
function sanitizeHTML(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// Use textContent instead of innerHTML
button.textContent = sanitizeHTML(config.company_name);
```

### 3. CORS Configuration

Backend API must set proper CORS headers:
```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, OPTIONS
Access-Control-Allow-Headers: X-API-Key, Content-Type
```

### 4. CSP Compatibility

Widget should work with strict CSP policies:
```html
<meta http-equiv="Content-Security-Policy"
      content="script-src 'self' https://cdn.luranai.com;
               frame-src https://chat.luranai.com;
               connect-src https://api.luranai.com;">
```

---

## Deployment Checklist

### Pre-Deployment

- [ ] Code reviewed and approved
- [ ] Unit tests passing (>80% coverage)
- [ ] Integration tests passing
- [ ] E2E tests passing on all browsers
- [ ] Bundle size < 15KB gzipped
- [ ] Performance tested (Lighthouse score >90)
- [ ] Accessibility tested (WCAG 2.1 AA)
- [ ] Security audit completed

### Deployment

- [ ] Build production bundle
- [ ] Upload to CDN with versioning
- [ ] Update DNS/CDN cache
- [ ] Test on staging environment
- [ ] Deploy to production
- [ ] Verify on multiple customer sites
- [ ] Monitor error logs

### Post-Deployment

- [ ] Monitor widget load errors
- [ ] Check API success rate
- [ ] Verify analytics tracking
- [ ] Customer feedback collection
- [ ] Performance monitoring (Core Web Vitals)

---

## Support and Maintenance

### Versioning Strategy

- **v1.x.x** - Current stable version
- **v2.x.x** - Major breaking changes (future)

### Update Process

1. Customer updates script src:
   ```html
   <script src="https://cdn.luranai.com/widget/v1/widget.js"></script>
   ```

2. Or pin to specific version:
   ```html
   <script src="https://cdn.luranai.com/widget/v1.0.0/widget.js"></script>
   ```

### Monitoring

**Metrics to track:**
- Widget load success rate
- API call success rate
- Average load time
- Error rate by browser
- User engagement (opens, messages)

**Tools:**
- Sentry (error tracking)
- Google Analytics (usage tracking)
- Datadog/New Relic (performance monitoring)

---

## Resources

### Documentation
- [Frontend API Documentation](./FRONTEND_API_DOCUMENTATION.md)
- [Backend API Reference](./API_REFERENCE.md)
- [User Roles and Permissions](./USER_ROLES.md)

### Development Tools
- [Webpack](https://webpack.js.org/)
- [Babel](https://babeljs.io/)
- [Playwright](https://playwright.dev/)

### External References
- [iframe Best Practices](https://developer.mozilla.org/en-US/docs/Web/HTML/Element/iframe)
- [postMessage API](https://developer.mozilla.org/en-US/docs/Web/API/Window/postMessage)
- [Web Accessibility](https://www.w3.org/WAI/WCAG21/quickref/)

---

## Contact

**Widget Development Team:**
- Email: widget@luranai.com
- Slack: #widget-development

**Questions or Issues:**
- Open issue on GitHub
- Contact backend team for API questions
- Contact design team for UX questions

---

**Last Updated:** 2026-01-15
**Version:** 1.0
**Maintained by:** Luran Widget Team
