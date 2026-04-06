# Luran B2C Implementation Blueprint

## Goal

Ship the first production-grade customer account layer on top of the existing Luran business-support stack without breaking the anonymous visitor flow that already powers the widget and hosted support pages.

## Current Reuse Strategy

- Keep `luran-dashboard` as the B2B/business workspace.
- Use `luran-chat` as the B2C customer application surface.
- Reuse the existing public business directory, hosted support page data, chat UI, RAG flow, escalation flow, and agent handoff infrastructure.
- Add a dedicated customer auth model instead of reusing tenant staff `users`.

## Architecture Position

### Identity

- `users`: business staff only
- `customer_users`: logged-in B2C customers
- `visitors`: anonymous browser identity for widget/hosted support

### Conversation Ownership

`conversation_sessions` should support both:

- `visitor_id` for anonymous/public support flows
- `customer_user_id` for logged-in customer flows

This keeps one support engine while supporting two entry modes.

## Delivery Phases

### Phase 1: Customer Foundation

- Add `customer_users`
- Add customer auth endpoints
- Add customer JWT dependency
- Add `customer_user_id` to `conversation_sessions`
- Add customer conversation access endpoints
- Add `luran-chat` customer auth pages
- Add logged-in customer shell with Messages, Explore, Channels, Profile, Settings

### Phase 2: Persistent Customer Messaging

- Route authenticated business-profile chat through customer endpoints
- Reuse `ChatWindow` with customer-specific message transport and websocket auth
- Add customer inbox list and conversation detail views
- Ensure same business maps to same customer thread

### Phase 3: Relationship Layer

- `customer_business_follows`
- follow/unfollow endpoints
- followed-business shortcuts in Explore/Profile
- share-business flow

### Phase 4: Channels Layer

- `channel_posts`
- customer channels feed
- channel detail
- business-side publishing workflow

## Backend Files to Extend First

- `app/db/models/conversation.py`
- `app/services/conversation_service.py`
- `app/api/deps.py`
- `app/api/v1/api.py`
- `app/api/v1/endpoints/chat.py`
- `app/api/v1/endpoints/websocket.py`

## Frontend Files to Reuse First

- `luran-chat/app/page.tsx`
- `luran-chat/app/components/HostedSupportPage.tsx`
- `luran-chat/app/components/ChatWindow.tsx`
- `luran-chat/app/hooks/useChat.ts`
- `luran-chat/app/hooks/useWebSocket.ts`

## UX Direction

Visual thesis: calm, trustworthy, messaging-first customer workspace with a polished support-product feel rather than a generic dashboard.

Interaction thesis:

- fast search and direct business entry
- stable inbox continuity
- subtle transitions and strong hierarchy, not heavy chrome

## Environment Notes

- `FRONTEND_URL` should remain the B2B/business app base URL used by `luran-dashboard`.
- `CUSTOMER_FRONTEND_URL` should point to the B2C/customer app base URL used by `luran-chat`.
- Customer verification and reset emails should always build links from `CUSTOMER_FRONTEND_URL` so they land on `/verify-email` and `/reset-password` inside `luran-chat`.

## Guardrails

- Do not mix customer auth with tenant staff auth.
- Do not remove visitor/browser continuity for public support.
- Do not create a second chat engine for B2C; extend the current one.
- Prefer reusing `luran-chat` components and only copy small proven UI primitives when necessary.
