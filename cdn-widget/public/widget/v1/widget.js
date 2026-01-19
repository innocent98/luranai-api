/**
 * Luran Widget Loader - Proof of Concept
 *
 * This is a minimal implementation to test the backend API integration.
 * Production version should be built by frontend team with proper error handling,
 * bundle optimization, and comprehensive browser support.
 *
 * Version: 1.0.0-poc
 * Last Updated: 2026-01-19
 */

(function() {
  'use strict';

  // Prevent multiple widget instances
  if (window.LuranWidget) {
    console.warn('[Luran Widget] Widget already loaded');
    return;
  }

  // Configuration from customer's website
  const settings = window.luranSettings || {};

  if (!settings.tenantId || !settings.apiKey) {
    console.error('[Luran Widget] Missing required settings: tenantId and apiKey');
    console.error('[Luran Widget] Expected format: window.luranSettings = { tenantId: "uuid", apiKey: "pk_live_..." }');
    return;
  }

  // API Configuration - Update these for production
  const API_BASE_URL = settings.apiUrl || 'http://localhost:8000/api/v1';  // Change to https://api.luranai.com/api/v1 in production
  const CHAT_UI_URL = settings.chatUrl || 'http://localhost:3000/widget';  // Change to https://chat.luranai.com/widget in production

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
      console.log('[Luran Widget] Initializing...');
      console.log('[Luran Widget] Tenant ID:', settings.tenantId);
      console.log('[Luran Widget] API URL:', API_BASE_URL);

      // Fetch tenant configuration
      config = await fetchConfig();

      if (!config) {
        console.error('[Luran Widget] Failed to load configuration');
        return;
      }

      console.log('[Luran Widget] Configuration loaded:', config);

      // Create floating button
      button = createFloatingButton();
      document.body.appendChild(button);

      // Add button click listener
      button.addEventListener('click', toggleChat);

      console.log('[Luran Widget] Widget initialized successfully');
    } catch (error) {
      console.error('[Luran Widget] Initialization error:', error);
    }
  }

  /**
   * Fetch widget configuration from API
   */
  async function fetchConfig() {
    try {
      const url = `${API_BASE_URL}/widget/public/config/${settings.tenantId}`;
      console.log('[Luran Widget] Fetching config from:', url);

      const response = await fetch(url, {
        headers: {
          'X-API-Key': settings.apiKey,
          'Content-Type': 'application/json'
        }
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`HTTP ${response.status}: ${errorText}`);
      }

      const data = await response.json();
      console.log('[Luran Widget] Config received:', data);
      return data;
    } catch (error) {
      console.error('[Luran Widget] Failed to fetch config:', error);

      // Return fallback config for testing
      console.warn('[Luran Widget] Using fallback configuration');
      return {
        tenant_id: settings.tenantId,
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
  }

  /**
   * Create floating button DOM element
   */
  function createFloatingButton() {
    const btn = document.createElement('button');
    btn.id = 'luran-widget-button';
    btn.setAttribute('aria-label', 'Open chat');
    btn.setAttribute('role', 'button');
    btn.setAttribute('tabindex', '0');

    // SVG chat icon
    btn.innerHTML = `
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 2C6.48 2 2 6.48 2 12C2 13.93 2.63 15.71 3.69 17.17L2.29 21.71L7.07 20.35C8.47 21.25 10.18 21.75 12 21.75C17.52 21.75 22 17.27 22 11.75C22 6.23 17.52 2 12 2Z" fill="white"/>
      </svg>
      ${config.show_unread_badge ? '<span class="luran-badge" style="display: none;">0</span>' : ''}
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
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    `;

    // Position based on config
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

    // Add badge styles if enabled
    if (config.show_unread_badge) {
      const style = document.createElement('style');
      style.textContent = `
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
      `;
      document.head.appendChild(style);
    }
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
    console.log('[Luran Widget] Opening chat');

    if (!iframe) {
      iframe = createChatIframe();
      document.body.appendChild(iframe);
    }

    iframe.style.display = 'block';
    isOpen = true;

    // Update button icon to close icon
    button.innerHTML = `
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M19 6.41L17.59 5L12 10.59L6.41 5L5 6.41L10.59 12L5 17.59L6.41 19L12 13.41L17.59 19L19 17.59L13.41 12L19 6.41Z" fill="white"/>
      </svg>
    `;

    // Reset unread count
    if (config.show_unread_badge) {
      updateUnreadBadge(0);
    }
  }

  /**
   * Close chat window
   */
  function closeChat() {
    console.log('[Luran Widget] Closing chat');

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
    frame.src = `${CHAT_UI_URL}?tenantId=${settings.tenantId}&apiKey=${settings.apiKey}`;
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
    // Security: Verify origin (in production, check against chat.luranai.com)
    // For POC, we'll accept localhost
    if (!event.origin.includes('localhost') && !event.origin.includes('luranai.com')) {
      return;
    }

    const { type, data } = event.data;

    console.log('[Luran Widget] Message from iframe:', type, data);

    switch (type) {
      case 'CLOSE_CHAT':
        closeChat();
        break;

      case 'MINIMIZE_CHAT':
        closeChat();
        break;

      case 'UPDATE_UNREAD':
        updateUnreadBadge(data.count);
        break;

      case 'SESSION_STARTED':
        console.log('[Luran Widget] Session started:', data.sessionId);
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
   * Public API
   */
  window.LuranWidget = {
    open: openChat,
    close: closeChat,
    toggle: toggleChat,
    getConfig: () => config,
    isOpen: () => isOpen,
    version: '1.0.0-poc'
  };

  // Initialize when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
