Perfect — this is now much clearer and much more focused.

What follows is a **production-ready B2C Dashboard PRD for Luran only**, written as a handoff-ready document for **UI/UX designers, frontend engineers, backend engineers, and product planning**.

This PRD assumes:

* **Luran B2B system already exists**
* **AI/RAG already exists**
* **Business discovery page already exists**
* **Widget and website-side chat already exist**
* What is being built now is the **logged-in customer-facing Luran platform experience**

---

# LURAN B2C DASHBOARD PRD

## Product Requirements Document

### Product: Luran Customer Platform (B2C)

### Version: v1.0

### Status: Production Scope

### Scope Type: Customer-Facing Dashboard Only

---

# 1. PRODUCT OVERVIEW

## 1.1 Product Summary

Luran B2C is the customer-facing side of the Luran platform that allows registered users to:

* discover businesses on Luran
* view business profiles
* start and continue conversations with businesses
* manage all business conversations from one inbox
* subscribe to business channels
* receive updates from businesses they follow
* keep a persistent communication history across businesses

This transforms Luran from only a business support infrastructure into a **customer communication hub**, where customers can interact with multiple businesses from one centralized platform.

---

# 2. PRODUCT GOAL

The goal of the Luran B2C dashboard is to provide users with a **centralized, trusted, and persistent communication layer** for interacting with businesses directly on Luran.

Instead of relying only on:

* website widgets
* email threads
* WhatsApp chats
* scattered business updates

Customers should be able to use **one Luran account** to:

* find businesses
* message them
* follow them
* receive updates from them
* revisit previous conversations at any time

---

# 3. PRIMARY USER TYPE

## Registered Customer User

A customer user is a person who creates a Luran account to interact with businesses listed on the platform.

This user is **not a tenant/business owner**.
This user is a **consumer or customer of tenant businesses**.

---

# 4. CORE USER VALUE

A customer should be able to say:

> “I can search businesses, chat with them, keep my conversations, and receive their updates all in one place.”

That is the core value proposition of the B2C platform.

---

# 5. PRODUCT OBJECTIVES

The B2C dashboard must enable users to:

1. Create and manage a customer account
2. Search and discover businesses
3. View public business profiles
4. Start and continue chat conversations with businesses
5. Maintain a persistent inbox across multiple businesses
6. View past communication history
7. Follow/subscribe to businesses
8. Receive business updates through channels
9. Manage profile and account preferences

---

# 6. IN SCOPE

This PRD includes:

* customer signup/login
* customer email verification
* customer dashboard shell
* business discovery integration
* business profile view
* follow / subscribe system
* messages / inbox
* conversation detail
* channels / updates feed
* customer profile
* account settings
* legal pages integration
* sharing behavior
* data relationships
* backend logic and state behavior

---

# 7. OUT OF SCOPE

This PRD excludes:

* business dashboard
* AI/RAG logic
* knowledge base setup
* team management
* billing logic for businesses
* widget implementation
* website-side anonymous visitor flow
* call / IVR
* mobile native app
* advanced social features (likes, comments, reactions, reposts)
* monetization or paid consumer features
* push notification system beyond basic architecture note

---

# 8. INFORMATION ARCHITECTURE

The Luran B2C dashboard should contain the following main areas:

### Core navigation structure

* Home / Explore
* Messages
* Channels
* Profile / Settings

Recommended nav placement:

* **desktop:** left sidebar
* **mobile web/PWA:** bottom navigation

---

# 9. FEATURE MODULES

---

# MODULE 1 — CUSTOMER AUTHENTICATION

## 9.1 Purpose

Allow customers to create an account and access a persistent personal dashboard.

## 9.2 Supported auth flow

### Signup

Customer signs up with:

* full name
* email
* password

### Verification

Customer verifies email through OTP or verification link

### Login

Customer logs in with:

* email
* password

### Password reset

Customer can request password reset via email

---

## 9.3 Functional requirements

### Signup page

Fields:

* Full Name
* Email Address
* Password
* Confirm Password
* Terms agreement checkbox

Actions:

* Create account
* Continue to verification

### Email verification page

Fields:

* verification code OR verification link flow

Actions:

* Verify account
* Resend verification

### Login page

Fields:

* Email
* Password

Actions:

* Login
* Forgot password

### Forgot password page

Fields:

* Email

Actions:

* Send reset link/code

---

## 9.4 Validation rules

* full name required
* email required and unique
* password minimum 8 chars
* email must be verified before full access
* duplicate account creation should be blocked

---

## 9.5 Acceptance criteria

* user can create account successfully
* user cannot access dashboard until email is verified
* user can log in and out successfully
* user can reset forgotten password

---

# MODULE 2 — CUSTOMER DASHBOARD SHELL

## 10.1 Purpose

Provide the logged-in framework that contains all major customer-facing product areas.

## 10.2 Dashboard sections

* Explore (Search businesses)
* Messages
* Channels
* Profile
* Settings
* Logout

---

## 10.3 UX requirements

Dashboard must:

* load fast
* feel familiar like a communication app
* prioritize message continuity and discovery
* support mobile-first responsive behavior

---

## 10.4 Acceptance criteria

* authenticated user lands in dashboard
* navigation works across all dashboard modules
* dashboard is responsive across desktop and mobile web

---

# MODULE 3 — BUSINESS DISCOVERY / EXPLORE

## 11.1 Purpose

Allow customers to discover and access businesses listed on Luran.

## 11.2 Entry point

Main CTA:

```text
Search a business
```

This can exist on:

* public landing page
* logged-in dashboard
* homepage/explore tab

---

## 11.3 Functional requirements

Customer should be able to:

* search businesses by name
* search by category (optional if data exists)
* browse recommended or featured businesses
* open business profile pages
* follow businesses
* start chat from business profile
* share business link

---

## 11.4 Search result card fields

Each business card should display:

* business logo
* business name
* category (if available)
* short description
* verification badge (optional/minimal/manual)
* follow button
* chat button
* share icon

---

## 11.5 Search states

States:

* default state
* loading state
* results state
* no result state
* empty query suggestions state

---

## 11.6 Acceptance criteria

* user can search for a business successfully
* search results are clickable
* opening a result leads to business profile
* user can start chat or follow directly from result/profile

---

# MODULE 4 — BUSINESS PROFILE VIEW

## 12.1 Purpose

Provide a dedicated customer-facing page for each business on Luran.

This is the page the customer sees before choosing to:

* follow
* share
* message
* view updates

---

## 12.2 Business profile fields

Must include:

* business logo
* business display name
* verification badge (if approved)
* business category
* business description/about
* support availability text
* website/social links (if available)
* Follow / Subscribe button
* Share button
* Start Chat button

Optional:

* number of subscribers/followers
* number of channel posts
* “Last active” or “usually replies within” later

---

## 12.3 Actions available

User can:

* follow / unfollow business
* start conversation
* share business profile
* view business channel updates

---

## 12.4 Acceptance criteria

* business profile loads from business ID or slug
* follow state persists
* chat CTA opens or creates conversation thread
* share action produces shareable URL

---

# MODULE 5 — FOLLOW / SUBSCRIBE SYSTEM

## 13.1 Purpose

Allow customers to subscribe to a business so they can receive future updates.

This is the foundation for channels.

---

## 13.2 Functional behavior

When a customer follows a business:

* the business is added to the customer’s followed list
* the customer becomes eligible to receive channel updates
* the business appears in customer’s followed business record
* future channel posts from that business appear in Channels

---

## 13.3 Follow states

* Not Following
* Following

Optional future:

* muted
* notifications off

---

## 13.4 Acceptance criteria

* user can follow a business
* user can unfollow a business
* follow state persists across sessions
* followed business channel posts appear in Channels

---

# MODULE 6 — MESSAGES / INBOX

## 14.1 Purpose

Provide a centralized customer inbox containing all business conversations.

This is one of the most important parts of the B2C product.

It should feel familiar, fast, and persistent — similar to the simplicity of WhatsApp or messaging inboxes.

---

## 14.2 Functional requirements

Customer should be able to:

* see a list of businesses they have chatted with
* see the most recent message preview
* see unread message indicators
* see latest conversation timestamp
* click any thread to continue conversation
* quickly return to previous chats without searching again

---

## 14.3 Conversation list item fields

Each message thread item should display:

* business logo
* business name
* last message preview
* unread indicator
* timestamp of last message
* optional resolved/open tag later

---

## 14.4 Inbox states

* no conversations yet
* loading
* populated list
* search within messages
* archived/resolved later optional

---

## 14.5 Acceptance criteria

* all prior customer-business chats are listed in inbox
* latest conversation appears at top
* clicking a thread opens conversation detail
* inbox persists across login sessions

---

# MODULE 7 — CONVERSATION DETAIL

## 15.1 Purpose

Allow the customer to view and continue a conversation with a specific business.

---

## 15.2 Functional requirements

Conversation page should include:

* business header
* business name/logo
* verification badge if applicable
* chat thread
* message input
* send button
* date separators
* loading history
* persistent message timeline

---

## 15.3 Message types supported (MVP)

* customer text message
* AI response message
* business human agent message
* system message (optional)

---

## 15.4 Required behavior

* if conversation already exists → open existing thread
* if conversation does not exist → create new thread
* conversation history must persist
* customer can continue previous discussion anytime
* messages should appear in chronological order
* latest messages should auto-scroll into view

---

## 15.5 Message metadata

Each message should include:

* sender type
* content
* timestamp

Optional future:

* delivered/read state
* attachment support
* voice notes
* media upload

---

## 15.6 Acceptance criteria

* customer can send a message to business
* message is stored and appears in thread
* customer can reopen old thread and continue
* same business always maps to same conversation thread unless business rules say otherwise

---

# MODULE 8 — CHANNELS

## 16.1 Purpose

Allow customers to receive business updates from followed/subscribed businesses.

This behaves like a **business update feed / newsletter stream**.

This is not a social network in MVP.

It is a **broadcast update layer**.

---

## 16.2 What a channel is

A channel is a stream of updates published by a business for subscribed customers.

Examples:

* “We’ll be upgrading our platform tonight”
* “New products are now available”
* “Customer support hours have changed”
* “Holiday delivery update”

---

## 16.3 Customer-side functional requirements

Customer should be able to:

* view updates from followed businesses
* open full update content
* see which business posted the update
* open business profile from channel update
* optionally unsubscribe from that business

---

## 16.4 Channel feed item fields

Each channel item should display:

* business logo
* business name
* update title
* update preview/snippet
* timestamp
* open/read CTA

Optional:

* category tag
* announcement badge

---

## 16.5 Feed states

* empty state (“Follow businesses to receive updates”)
* loading
* populated feed

---

## 16.6 Acceptance criteria

* followed business posts appear in customer channel feed
* customer can open full update
* customer can navigate to business profile from update
* unfollowed business updates no longer appear

---

# MODULE 9 — CUSTOMER PROFILE

## 17.1 Purpose

Allow customers to manage their personal account information.

---

## 17.2 Profile fields

Customer profile should include:

* full name
* email address
* profile photo (optional, can be phase 2)
* account created date
* followed businesses count (optional)
* conversations count (optional)

---

## 17.3 Actions

Customer can:

* update full name
* change password
* update profile photo (optional later)
* view email verification status

---

## 17.4 Acceptance criteria

* customer can update profile successfully
* changes persist immediately
* profile reflects correct account details

---

# MODULE 10 — SETTINGS

## 18.1 Purpose

Provide customer-level account and app preferences.

---

## 18.2 Settings items

Should include:

* account settings
* notification preferences (placeholder/basic)
* privacy settings (basic)
* legal links
* logout
* delete account request (optional but recommended)

---

## 18.3 Legal pages

Must link to:

* Terms of Use
* Privacy Policy
* Cookie Policy (if applicable)
* Luran legal/help pages

You specifically mentioned:

> “Legals, terms of use — this is going to be Luran legals”

So these should be visible from settings.

---

## 18.4 Acceptance criteria

* legal links open correctly
* logout works correctly
* account settings are accessible from settings

---

# MODULE 11 — SHARE BUSINESS

## 19.1 Purpose

Allow users to share a business profile with others.

---

## 19.2 Functional behavior

Customer should be able to share:

* business public profile URL
* optionally direct chat/start page URL later

Entry points:

* business profile
* search results
* channels

---

## 19.3 Acceptance criteria

* clicking share generates correct shareable business URL
* shared URL opens correct business profile

---

# MODULE 12 — CUSTOMER DATA MODEL

Below is the minimum recommended backend model for B2C.

---

## 20.1 `customer_users`

| Field             | Type              | Notes        |
| ----------------- | ----------------- | ------------ |
| id                | uuid              | primary key  |
| full_name         | string            | required     |
| email             | string            | unique       |
| password_hash     | string            | secure auth  |
| email_verified_at | datetime nullable | verification |
| avatar_url        | string nullable   | optional     |
| created_at        | datetime          | audit        |
| updated_at        | datetime          | audit        |

---

## 20.2 `customer_business_follows`

| Field                      | Type     | Notes            |
| -------------------------- | -------- | ---------------- |
| id                         | uuid     | primary key      |
| customer_user_id           | uuid     | FK               |
| business_id / workspace_id | uuid     | tenant reference |
| followed_at                | datetime | timestamp        |

Unique constraint:

```text
(customer_user_id, workspace_id)
```

---

## 20.3 `customer_conversations`

If your conversation system already exists, reuse it rather than duplicating tables.

Likely mapping should become:

| Field            | Type          | Notes                  |
| ---------------- | ------------- | ---------------------- |
| id               | uuid          | primary key            |
| workspace_id     | uuid          | business               |
| customer_user_id | uuid nullable | logged-in customer     |
| visitor_id       | uuid nullable | anonymous visitor flow |
| status           | enum          | open/resolved etc      |
| created_at       | datetime      | audit                  |
| updated_at       | datetime      | audit                  |

Important:
This allows Luran to support **both**:

* logged-in B2C customers
* anonymous widget visitors

That is the cleanest architecture.

---

## 20.4 `channel_posts`

| Field        | Type              | Notes          |
| ------------ | ----------------- | -------------- |
| id           | uuid              | primary key    |
| workspace_id | uuid              | business owner |
| title        | string            | required       |
| body         | text              | content        |
| is_published | boolean           | visibility     |
| published_at | datetime nullable | timestamp      |
| created_at   | datetime          | audit          |
| updated_at   | datetime          | audit          |

---

## 20.5 `channel_feed_reads` (optional later)

Tracks whether a customer has opened/read a channel post.

| Field            | Type     | Notes       |
| ---------------- | -------- | ----------- |
| id               | uuid     | primary key |
| customer_user_id | uuid     | FK          |
| channel_post_id  | uuid     | FK          |
| read_at          | datetime | timestamp   |

---

# 13. USER JOURNEYS

---

# Journey 1 — Customer discovers and chats with a business

1. Customer visits Luran
2. Clicks **Search a business**
3. Searches for business
4. Opens business profile
5. Clicks **Start Chat**
6. If not logged in, prompted to sign up or log in
7. Completes auth
8. Conversation thread opens
9. Customer sends message
10. Business replies via AI or agent
11. Thread appears in customer Messages inbox

**Success outcome:**
Customer can easily discover and message a business from Luran platform.

---

# Journey 2 — Returning customer continues prior conversation

1. Customer logs into Luran
2. Opens Messages
3. Sees list of businesses previously chatted with
4. Clicks one thread
5. Previous messages load
6. Customer continues conversation

**Success outcome:**
Customer gets persistent communication continuity without needing to search again.

---

# Journey 3 — Customer follows a business and receives updates

1. Customer opens business profile
2. Clicks Follow / Subscribe
3. Business later publishes update
4. Update appears in Channels tab
5. Customer opens and reads update

**Success outcome:**
Customer receives structured updates from businesses they care about.

---

# Journey 4 — Customer shares a business

1. Customer opens business profile
2. Clicks Share
3. Shared URL is copied or shared
4. Recipient opens business page

**Success outcome:**
Business discovery can spread organically through customers.

---

# 14. USE CASES

### Customer use cases

* As a customer, I want to create an account so I can keep my business conversations in one place.
* As a customer, I want to search for businesses so I can contact them directly from Luran.
* As a customer, I want to see all my prior business chats in one inbox so I can quickly continue them.
* As a customer, I want to follow businesses so I can receive their updates.
* As a customer, I want to read channel updates so I can stay informed.
* As a customer, I want to share a business profile so I can recommend it to someone else.

---

# 15. SCREEN INVENTORY (UI/UX HANDOFF)

## Public / Auth

* Customer signup
* Email verification
* Customer login
* Forgot password
* Reset password

## Dashboard

* Dashboard shell / navigation
* Explore / Search businesses
* Search results
* Business profile
* Messages list
* Conversation detail
* Channels feed
* Channel detail
* Customer profile
* Settings / legal

---

# 16. UI/UX PRINCIPLES

The B2C dashboard should feel:

* clean
* lightweight
* familiar
* messaging-first
* mobile-friendly
* trustworthy

### Design emphasis

* minimize friction
* keep conversation continuity obvious
* make businesses easy to discover
* make channels feel informative, not noisy
* avoid overcomplicating with social-media-like clutter

### Product style reference

Closest UX feel should be a blend of:

* WhatsApp inbox simplicity
* LinkedIn business page clarity
* lightweight newsletter/feed experience

---

# 17. BACKEND / ENGINEERING NOTES

## 17.1 Important architecture recommendation

Your current conversation system should support two sender identity types:

### Anonymous flow

Used by business website widget:

* `visitor_id`

### Logged-in B2C flow

Used by Luran customer dashboard:

* `customer_user_id`

This gives Luran flexibility without duplicating the support infrastructure.

---

## 17.2 Recommended identity logic

If a logged-in customer starts chat from Luran platform:

```text
conversation.customer_user_id = current_user.id
conversation.visitor_id = null
```

If anonymous website widget visitor starts chat:

```text
conversation.customer_user_id = null
conversation.visitor_id = generated_visitor_id
```

This is the cleanest and most scalable model.

---

## 17.3 Channel logic recommendation

Only followed businesses should appear in Channels feed.

Feed query should return:

```text
channel_posts
WHERE workspace_id IN (
   businesses followed by current customer
)
ORDER BY published_at DESC
```

---

# 18. SECURITY / COMPLIANCE NOTES

* secure password hashing required
* email verification required
* protect customer-business message privacy
* ensure tenant data isolation
* customers must not access conversations belonging to another customer
* channel posts must only be fetched from allowed business records
* all auth endpoints must be rate-limited

---

# 19. SUCCESS METRICS

Recommended B2C metrics to track:

### Acquisition

* customer signup conversion rate
* email verification completion rate

### Discovery

* search-to-profile open rate
* profile-to-chat conversion rate
* profile-to-follow conversion rate

### Messaging

* number of customer-started conversations
* returning customer conversation rate
* messages per active customer

### Channels

* follow rate
* channel open rate
* repeat visit rate to Channels

### Retention

* 7-day customer retention
* 30-day returning customer rate

---

# 20. MVP DELIVERY PRIORITY

Recommended implementation order:

## Phase 1 — Core customer account + messaging

1. customer auth
2. dashboard shell
3. business discovery integration
4. business profile
5. messages list
6. conversation detail

## Phase 2 — Relationship layer

7. follow / subscribe system
8. share business
9. profile + settings

## Phase 3 — Business updates layer

10. channels feed
11. channel detail
12. legal/help integration

---

# FINAL PRODUCT DEFINITION

The Luran B2C dashboard should be defined internally as:

> **A customer-facing communication dashboard that lets users discover businesses, message them, follow them, and receive updates — all from one account.**

That is the sharpest and most production-accurate definition of this system.

---

