# Offerings catalog: FE integration guide, part 2 (chat, quote, lead, alerts)

**Status:** AI path (stream S2) built and exercised on 2026-10-31 on branch `feature/offerings-ai-grounding` (draft PR #197, stacked on S1's #195). Part 1 (`docs/fe-integration-guide-offerings.md`) covers the REST API the dashboard builds against; this part covers what the **chat** returns and what the **lead** and **notifications** now carry.

**Every body below is copied from a capture of the running app** (API + arq worker + Postgres 15 + Redis + the fake WhatsApp sidecar), stored in `docs/fe-integration-captures/offerings-ai/`; the file number is cited under each block. Re-derive any claim with `scripts/capture_offerings_ai.py` (instructions in its header). Ids and timestamps come from a throwaway database; tokens are `<jwt-placeholder>`; tenants and data are fake.

**What was NOT real in the live run (read this before trusting the wording of any reply):** the language model was a **deterministic stand-in** (`tests/fakes/live_stub_offerings.py`, built on `scripts/offerings_eval.py`). It reads the catalog block the real code injected and answers from it, and it misbehaves on its first attempt when a customer haggles so the guard shows up. So the **shape** of every response, the guard, the cart, the quote, the lead and the alerts are real; the **wording** of replies is not what a real model says. Cloudinary and image moderation were stand-ins (photo URLs are made up). Voice, strict (fail-closed) mode and the WhatsApp photo/quote-text messages were not run live (see section 11). `token_breakdown` numbers in these captures come from the stand-in and are meaningless (a negative `completion_tokens` is a stand-in artefact); ignore them.

## 1. What the feature is, in the FE's terms

When a tenant has at least one **active** item and has not switched the catalog off, the AI answers "what do you have / how much / is it in stock" from the catalog, read live from the database, on every channel (web widget, hosted page, WhatsApp, voice). The chat response then carries **cards** to render (`meta.offerings`). When a customer chooses items, the backend (not the model) writes a **lead with wanted items**, may attach a **quote card** (`meta.quote`), and alerts the owner. Prices are never trusted from the model: a code guard checks every reply and replaces a reply that states a price that is not in the catalog.

**Nothing changes for a tenant with no active items**: the chat response has **no `offerings` key at all** and the prompt is byte-identical to before. Treat "key absent" and "key present" as two different states (section 3).

There is **no new endpoint** in this part. What changes:

| Where | What is new |
|---|---|
| `POST /api/v1/chat/message` (and the customer-token and voice routes that share it) | `message.meta.offerings`; `message.meta.quote` can now come from the catalog |
| `GET /api/v1/leads/{id}` | `data.interests[]` filled by the AI path (shape defined in part 1); `source` follows the channel; two new score rows |
| `GET /api/v1/notifications` | new event `offering_request` |
| `GET /api/v1/offerings/misses` (part 1) | now written by the AI path |

## 2. When the AI does what (what the FE will see)

| Customer says | Reply | `meta.offerings.mode` | Cards | Extra |
|---|---|---|---|---|
| "What do you have?" | the menu (up to 12 items) | `browse` | up to 5, those the reply names | |
| Names an item / asks a price | the item at its live price | `detail` | the items the reply is about | |
| Asks for something not listed | "not listed", offers the team | `none` | none | the term is counted in Not-found |
| Asks for a sold-out item | says it is sold out | `detail` | the item (`availability: "out_of_stock"`) | never offered as available |
| Haggles on a listed price | the listed price stands | `detail` | | `guard.blocked` may be true |
| Offers a price on a **negotiable** item | "the owner is open to offers" | `detail` | | offer stored on the lead; human offered; **no quote** |
| Chooses items (sales agent on) | confirms them back | `detail` | | lead + `interests` + owner alert (+ quote when allowed) |

## 3. `meta.offerings`

Present on an assistant message **only when the tenant had a catalog block injected for that turn**. Shape (frozen cross-stream contract):

| Field | Type | Notes |
|---|---|---|
| `mode` | `"browse"` \| `"detail"` \| `"none"` | `none` also covers "the lookup failed and the reply was price-free" (not run live) |
| `items[]` | array, **0 to 5** | the items **the reply is about**, from rows read this turn |
| `items[].ref` | string | `"#12"`, the per-business item number the owner sees |
| `items[].offering_id` | uuid | |
| `items[].name` | string | |
| `items[].price_text` | string | **already formatted**: `"₦12,000"`, `"from ₦5,000 per phone"`, `"price on request"`. Do not format or parse it |
| `items[].availability` | `"in_stock"` \| `"limited"` \| `"out_of_stock"` \| `"on_request"` | |
| `items[].cover_image_id` | uuid or `null` | |
| `items[].cover_url` | string or `null` | the **400 px card rendition** of the cover (not the stored original) |
| `items[].photo_count` | integer | 0 when none |
| `shown_at` | ISO 8601 with offset | when the catalog was read. **Cards are a snapshot:** show "as of <time>" when older than 1 hour |
| `guard` | object | `{checked, blocked, regenerated, fell_back}`, all booleans; for the pilot audit, not for display |

Browse (capture `01`, `meta` shown without the unchanged retrieval and token fields):

```json
{
  "escalation_action": "no_escalation",
  "offerings": {
    "mode": "browse",
    "items": [
      {
        "ref": "#1",
        "offering_id": "091fff84-9e60-48aa-9372-514f62f9b8eb",
        "name": "Canvas sneaker black",
        "price_text": "₦12,000",
        "availability": "in_stock",
        "cover_image_id": "98e477cd-c428-43de-828b-0ab70d098feb",
        "cover_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/3ea71d88-0c4f-4502-92cd-00a2ba818eca/4a4d6cadda58434a9aecde2615e2316e.jpg",
        "photo_count": 2
      },
      {
        "ref": "#2",
        "offering_id": "3056fcaa-ecc4-4ff9-853a-a666d0d0eb9f",
        "name": "Canvas sneaker white",
        "price_text": "₦13,500",
        "availability": "in_stock",
        "cover_image_id": null,
        "cover_url": null,
        "photo_count": 0
      },
      {
        "ref": "#3",
        "offering_id": "1730df2f-2ac6-4e46-83bd-72aea856d96e",
        "name": "Leather bag",
        "price_text": "₦45,000",
        "availability": "in_stock",
        "cover_image_id": null,
        "cover_url": null,
        "photo_count": 0
      },
      {
        "ref": "#4",
        "offering_id": "b3ef93c6-2eae-42c1-a642-426d4f7da0cd",
        "name": "Phone repair",
        "price_text": "from ₦5,000 per phone",
        "availability": "in_stock",
        "cover_image_id": null,
        "cover_url": null,
        "photo_count": 0
      },
      {
        "ref": "#5",
        "offering_id": "53ac4613-bdd8-4168-bdb9-04fa1337b978",
        "name": "Beach sandals",
        "price_text": "₦8,000",
        "availability": "out_of_stock",
        "cover_image_id": null,
        "cover_url": null,
        "photo_count": 0
      }
    ],
    "shown_at": "2026-10-31T00:25:02.359284+00:00",
    "guard": {
      "checked": true,
      "blocked": false,
      "regenerated": false,
      "fell_back": false
    }
  }
}
```

A single item with two photos (capture `02`; the reply text is `"The Canvas sneaker black is ₦12,000, in stock."`):

```json
{
  "mode": "detail",
  "items": [
    {
      "ref": "#1",
      "offering_id": "091fff84-9e60-48aa-9372-514f62f9b8eb",
      "name": "Canvas sneaker black",
      "price_text": "₦12,000",
      "availability": "in_stock",
      "cover_image_id": "98e477cd-c428-43de-828b-0ab70d098feb",
      "cover_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/3ea71d88-0c4f-4502-92cd-00a2ba818eca/4a4d6cadda58434a9aecde2615e2316e.jpg",
      "photo_count": 2
    }
  ],
  "shown_at": "2026-10-31T00:25:02.647717+00:00",
  "guard": {
    "checked": true,
    "blocked": false,
    "regenerated": false,
    "fell_back": false
  }
}
```

Not listed (capture `04`; reply `"I don't have that listed. Would you like me to connect you with the team?"`): note `mode: "none"` and **no cards**.

```json
{
  "mode": "none",
  "items": [],
  "shown_at": "2026-10-31T00:25:02.779890+00:00",
  "guard": {
    "checked": true,
    "blocked": false,
    "regenerated": false,
    "fell_back": false
  }
}
```

### Field-nesting traps

- **Cards are not the cart.** `items[]` lists what the *reply* is about, not what the customer has chosen. In capture `11` the customer already had two black sneakers and added a white one: the card list holds only the white sneaker while `meta.quote` holds both lines. Never derive the cart from the cards.
- **`meta.offerings` absent vs present.** A tenant with no active items (or the catalog switched off) has no `offerings` key. A tenant with items has it on **every** assistant reply, including replies with `items: []`.
- `cover_url` and `cover_image_id` are `null` (present, not absent) when the item has no photo.
- `meta.quote` in a **chat response** has only `{currency, line_items, total}`: no `id`, no `status`. The same quote read from the lead detail endpoint has `id`, `status` and `created_at`.
- The lead detail keeps its own envelope `{ "success": true, "data": ... }`; the chat response is un-enveloped.

## 4. The reply may not be what the model first wrote (price guard)

Every reply built on a catalog block is checked by code. If the first reply states an amount that is not the item's listed price, the backend asks the model **once more**; if that also fails it sends a reply **built from the database rows**. The customer only ever sees the final text. The FE needs to know two things:

1. A guarded turn takes longer (a second model call). Show your normal typing indicator; do not time out early.
2. A fallback reply is a plain list followed by an offer of the team, and the response then carries `escalation_action: "offer_escalation"`. Treat it exactly like the existing "would you like to talk to someone" consent step (the customer answers yes/no in the next message; nothing else to build).

Regenerated (capture `06`; the customer asked for ₦10,000 on a fixed-price item; final reply `"The Canvas sneaker black is listed at ₦12,000. I've noted your request.\n\nThe listed price for the Canvas sneaker black is ₦12,000 and it is firm."`; the last sentence is written by code, see section 5):

```json
{
  "checked": true,
  "blocked": true,
  "regenerated": true,
  "fell_back": false
}
```

Fell back to the code reply (capture `07`):

```json
{
  "escalation_action": "offer_escalation",
  "offerings": {
    "mode": "detail",
    "items": [
      {
        "ref": "#2",
        "offering_id": "3056fcaa-ecc4-4ff9-853a-a666d0d0eb9f",
        "name": "Canvas sneaker white",
        "price_text": "₦13,500",
        "availability": "in_stock",
        "cover_image_id": null,
        "cover_url": null,
        "photo_count": 0
      },
      {
        "ref": "#1",
        "offering_id": "091fff84-9e60-48aa-9372-514f62f9b8eb",
        "name": "Canvas sneaker black",
        "price_text": "₦12,000",
        "availability": "in_stock",
        "cover_image_id": "98e477cd-c428-43de-828b-0ab70d098feb",
        "cover_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/3ea71d88-0c4f-4502-92cd-00a2ba818eca/4a4d6cadda58434a9aecde2615e2316e.jpg",
        "photo_count": 2
      }
    ],
    "shown_at": "2026-10-31T00:25:02.953752+00:00",
    "guard": {
      "checked": true,
      "blocked": true,
      "regenerated": true,
      "fell_back": true
    }
  }
}
```

The reply text of capture `07` is:

```text
Here is what we have listed:
#2 Canvas sneaker white - ₦13,500 - in stock
#1 Canvas sneaker black - ₦12,000 - in stock
Would you like me to connect you with the team?

The listed price for the Canvas sneaker white is ₦13,500 and it is firm.
```

## 5. Price offers (the AI never agrees to a lower price)

The AI never agrees to, counters or hints at a price below the listed one. For a **negotiable** item it says the owner is open to offers, records the customer's figure on the lead, and hands the conversation to a human. Capture `08` (customer: "I'll pay 30k for the leather bag"; the reply ends with a line written **by code**, never by the model: `I've noted your offer of ₦30,000 for the Leather bag.`, after the model's own wording and the offer of the team). The model may not repeat the customer's figure beside an item name, nor agree, accept or discount: the guard blocks it. Code only says "noted your offer" when a human will pick it up (a negotiable item, or the customer insists); for a first offer on a non-negotiable item it states the firm price instead (`The listed price for the Canvas sneaker black is ₦12,000 and it is firm.`, captures `06` and `07`). The response of capture `08` also hands the conversation to a human **now**: `should_escalate` is `true`, `ticket_id` is set and `escalation_action` is `auto_escalate`; from then on the existing escalated-conversation behaviour applies (covered by `tests/api/v1/test_chat_escalated_suppression.py`; a later customer message was not sent in the live run). The model's text says `Would you like me to connect you with the team?` only because the stand-in wrote it; the line after it, `I'm handing this to the team now; they'll take it from here.`, is added by code:

```json
"Customer made a price offer"
```

```json
true
```

The stored offer, as the owner sees it on the lead (capture `09`, `data.interests`):

```json
[
  {
    "ref": "#3",
    "name": "Leather bag",
    "price_mode": "fixed",
    "price": 45000,
    "price_note": null,
    "qty": 1,
    "offered_price": 30000
  }
]
```

For a **non-negotiable** item the first offer is also stored but the listed price stands and no human is forced; if the customer insists on the same item again, the conversation is handed over like above. **No quote is made while an offer is pending, on this turn or any later one** (a human prices it): a later turn with the same cart gets no quote card and the reply ends with `A team member will confirm the price.` The figure must be one the customer wrote in their **current** message; an offer the model invents, a sold-out item, or an amount above ₦10,000,000,000 is ignored.

An offer made **after** the first cart (capture `23`) is recorded, hands over, makes no new quote, and raises its own owner alert titled `Price offer` (capture `24`, newest first), because the once-per-lead cart alert would otherwise swallow it:

```json
"The owner is open to offers on the Leather bag. I've noted your offer; the team will follow up. Would you like me to connect you with the team?\n\nI've noted your offer of ₦30,000 for the Leather bag.\n\nI'm handing this to the team now; they'll take it from here."
```

```json
{
  "id": "f57d1709-39da-4575-8cff-57a2d35a03b2",
  "event": "offering_request",
  "title": "Price offer",
  "body": "A customer offered ₦30,000 for Leather bag (listed ₦45,000). 2 × Canvas sneaker black (listed ₦12,000); 1 × Canvas sneaker white (listed ₦13,500); 1 × Leather bag (listed ₦45,000), customer offered ₦30,000",
  "read_at": null,
  "created_at": "2026-10-31T00:25:08.144117+00:00"
}
```

## 6. Wanted items, the quote card and the owner alert

When the Sales Agent is on and a customer chooses items, the backend writes the lead's wanted items and alerts the owner once. The quote card appears only when **all** of these hold: the plan includes quotes (`sales_close`), the tenant has quotes switched on, every chosen item has a **fixed** price, no price offer is pending on the lead, every line of the cart could be resolved, and the total fits the quote column (about ₦2.1 billion). A cart over that limit makes no quote, and code adds `This order is too large for me to total; a team member will confirm the price.` and hands the conversation over. A `from` or `on request` item gives wanted items and the alert but **no quote card** (a person prices it).

First cart (capture `10`, "I want 2 black sneakers"):

```json
{
  "currency": "NGN",
  "line_items": [
    {
      "label": "Canvas sneaker black",
      "detail": "2 × ₦12,000",
      "amount": 24000
    }
  ],
  "total": 24000
}
```

The cart changed to include a white sneaker, so a **new** quote is made (capture `11`, `meta.quote`):

```json
{
  "currency": "NGN",
  "line_items": [
    {
      "label": "Canvas sneaker black",
      "detail": "2 × ₦12,000",
      "amount": 24000
    },
    {
      "label": "Canvas sneaker white",
      "detail": "1 × ₦13,500",
      "amount": 13500
    }
  ],
  "total": 37500
}
```

The customer confirms without changing anything (capture `12`): **no `quote` key** in `meta`, and no new quote or alert. Render the card you already have; do not expect one on every turn.

A `from`-priced line (capture `13`, "I'll take the phone repair"): wanted items are recorded, **no `quote`** in `meta`:

```json
{
  "escalation_action": "no_escalation",
  "offerings": {
    "mode": "detail",
    "items": [
      {
        "ref": "#4",
        "offering_id": "b3ef93c6-2eae-42c1-a642-426d4f7da0cd",
        "name": "Phone repair",
        "price_text": "from ₦5,000 per phone",
        "availability": "in_stock",
        "cover_image_id": null,
        "cover_url": null,
        "photo_count": 0
      }
    ],
    "shown_at": "2026-10-31T00:25:03.495384+00:00",
    "guard": {
      "checked": true,
      "blocked": false,
      "regenerated": false,
      "fell_back": false
    }
  }
}
```

**A cart line that cannot be found never disappears silently.** Items must have been shown or chosen earlier in this conversation (or be in this turn's list) and still be active. When a line cannot be resolved (unknown, removed, or sold out), the earlier wanted items are kept, no quote is made, and code adds a sentence such as `I couldn't add Beach sandals: it is sold out.` or `I couldn't add #99: it isn't something I can find in this chat's list.`

**Totals are computed by code** as the sum of quantity times the price read from the database after the reply; the model never states a total. The detail text of each line is `"<qty> × <price>"` (a price note such as `per cut` is appended when the item has one).

## 7. The lead

`GET /api/v1/leads/{id}` after the first cart (capture `14`, `data`; long fields trimmed to the ones this feature touches):

```json
[
  {
    "ref": "#1",
    "name": "Canvas sneaker black",
    "price_mode": "fixed",
    "price": 12000,
    "price_note": null,
    "qty": 2,
    "offered_price": null
  },
  {
    "ref": "#2",
    "name": "Canvas sneaker white",
    "price_mode": "fixed",
    "price": 13500,
    "price_note": null,
    "qty": 1,
    "offered_price": null
  }
]
```

- `interests[]` is the latest non-empty cart. A later cart **replaces** the selection (an item the customer drops disappears, an earlier offer on a kept item carries over). An empty cart never clears it.
- Snapshots: name, price mode, price and note are what the customer was shown, copied at that time; a later edit or delete of the item never rewrites them.
- **Score rows:** two rows exist only when fired and are **not** in the usual nine: `selected_items` ("Chose items from the catalog", weight 15) and `price_offer` ("Made a price offer", weight 5). Do not assume `score_breakdown` always has nine rows. Capture `14` shows the fired row:

```json
{
  "key": "selected_items",
  "label": "Chose items from the catalog",
  "weight": 15,
  "fired": true
}
```

- **Lead source follows the channel.** Web widget `chat_widget`, hosted page `hosted_page`, WhatsApp `whatsapp`, voice `voice` (it used to be always `chat_widget`). The field is `data.source.landing_page` (and `source_label`). Capture `14` (web): `chat_widget`; capture `22` (WhatsApp): `whatsapp`.

```json
{
  "landing_page": "whatsapp",
  "utm": null,
  "channel": null,
  "first_seen": "2026-10-31T00:25:04.948838+00:00"
}
```

## 8. Notifications

The first committed cart of a conversation raises the new event `offering_request`; a quote raises `quote_generated` as before. Capture `15` (`GET /api/v1/notifications`, the two newest items):

```json
{
  "id": "4b60e94b-4211-4cc8-ab0f-85f233f24780",
  "event": "offering_request",
  "title": "New item request",
  "body": "A customer · 1 × Phone repair (listed ₦5,000)",
  "read_at": null,
  "created_at": "2026-10-31T00:25:03.539985+00:00"
}
```

```json
{
  "id": "324fcdb4-88f9-4582-95fc-7b87ce3fb5a4",
  "event": "quote_generated",
  "title": "New quote",
  "body": "₦37,500 · a prospect",
  "read_at": null,
  "created_at": "2026-10-31T00:25:03.333173+00:00"
}
```

In-app and Slack bodies carry the items with their listed prices and any figure the customer offered, in the tenant's currency (`A customer · 2 × Canvas sneaker black (listed ₦12,000)`). Slack text is escaped (`<!channel>` and links in a customer's name cannot fire). The CRM payload gets an `interests` list (`ref, name, qty, price, offered_price`). The conversation link of spec 8.5 is the existing dashboard link; a per-conversation deep link was not built (route unknown).

The e-mail (when the tenant's alert e-mail is on) lists the wanted items with the listed price and any figure the customer offered; Slack and CRM follow the same plan gate as quote alerts. The in-app text names the customer only when the lead already has a name (here the stand-in captured none, hence "A customer").

## 9. Support-mode tenants (Sales Agent off)

They get the same grounded answers and cards, but **no lead, no wanted items, no recorded offer and no alert**. Capture `16` has `meta.offerings`; capture `17` counts the leads in the database:

```json
0
```

The dashboard should tell such a tenant "Turn on the Sales Agent to capture wanted items and offers".

## 10. WhatsApp

Same pipeline, same `meta.offerings` on the stored assistant message. What the customer's phone receives is only the reply **text**; the cards and the quote card never reach WhatsApp. The reply is plain text with no markdown. What the fake sidecar received for "What do you have?" and "I want 2 black sneakers" (capture `20`):

```json
[
  {
    "key": "<session-key-placeholder>",
    "to_ref": "198765432111@lid",
    "text": "Here is what we have:\n#1 Canvas sneaker black - ₦12,000\n#2 Canvas sneaker white - ₦13,500\n#3 Leather bag - ₦45,000\n#4 Phone repair - from ₦5,000 per phone\n#5 Beach sandals - ₦8,000"
  },
  {
    "key": "<session-key-placeholder>",
    "to_ref": "198765432111@lid",
    "text": "Noted: 2 × Canvas sneaker black. What name should I put down?"
  }
]
```

The conversation as an agent sees it (capture `21`, last assistant message): `meta.channel` is `"whatsapp"` and the message carries `meta.offerings` and `meta.quote`:

```json
{
  "escalation_action": "no_escalation",
  "offerings": {
    "mode": "detail",
    "items": [
      {
        "ref": "#1",
        "offering_id": "091fff84-9e60-48aa-9372-514f62f9b8eb",
        "name": "Canvas sneaker black",
        "price_text": "₦12,000",
        "availability": "in_stock",
        "cover_image_id": "98e477cd-c428-43de-828b-0ab70d098feb",
        "cover_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/3ea71d88-0c4f-4502-92cd-00a2ba818eca/4a4d6cadda58434a9aecde2615e2316e.jpg",
        "photo_count": 2
      }
    ],
    "shown_at": "2026-10-31T00:25:05.845851+00:00",
    "guard": {
      "checked": true,
      "blocked": false,
      "regenerated": false,
      "fell_back": false
    }
  },
  "quote": {
    "currency": "NGN",
    "line_items": [
      {
        "label": "Canvas sneaker black",
        "detail": "2 × ₦12,000",
        "amount": 24000
      }
    ],
    "total": 24000
  },
  "channel": "whatsapp"
}
```

Catalog **photos** and the **quote as a second text message** on WhatsApp belong to another stream (S4b) and are **not** in this branch: until it lands, a WhatsApp customer sees the text only.

## 11. Verification table

| Behaviour | Verified |
|---|---|
| Shape of `meta.offerings` (browse, detail with photos, none) | **Live** (captures 01, 02, 04), model stand-in |
| `cover_url` is the 400 px rendition | **Live** (capture 02; a first run showed the stored original, fixed and re-captured) |
| Guard regeneration and code fallback, `guard` flags, `offer_escalation` | **Live** with a stand-in that misbehaves on cue (06, 07). Behaviour against the real model: **not verified** (the eval gate on staging does that) |
| Offer on a negotiable item stored, conversation handed over (`auto_escalate`, ticket), no quote | **Live** (08, 09) |
| Offer after the cart: separate `Price offer` alert, no new quote | **Live** (23, 24) |
| Overflow past the quote column, rollback of every non-fatal step, dropped/unresolved lines, pending-offer quote block, lead row lock | Unit and endpoint tests; the lock was checked against real Postgres (`tests/offerings_ai/test_lead_lock.py`). Not run live |
| Two parallel turns on one lead | Row lock proven on Postgres; a true concurrent chat run was **not** done |
| Cart to lead, `interests`, quote totals, no second quote when unchanged, new quote when changed | **Live** (10, 11, 12, 14) |
| `from` line gives no quote | **Live** (13) |
| Lite plan or quotes-off gives wanted items and no quote | Unit tests only (no second plan tenant in the live run) |
| Notification event and text | **Live** (15); e-mail body: unit test only (no SMTP in the live run) |
| Tenant with no active items: no `offerings` key and a byte-identical prompt | Unit tests only (a pinned golden prompt); every tenant in the live run had items |
| Support-mode tenant records nothing | **Live** (16, 17) |
| WhatsApp text path, `source: whatsapp`, plain text | **Live** with the fake sidecar (20, 21, 22). The real sidecar: **not verified** |
| WhatsApp photos and quote text message | **Not built here** (S4b) |
| Voice | **Not run** (same `send_message`, same guard; TTS reading of `₦` untested) |
| Fail-closed mode (lookup failed): no price in the reply | Unit and endpoint tests only; cannot be triggered live without fault injection |
| Stale-list caveat ("as of the owner's last update") | Unit tests on the block text only |
| Hosted-page `source` value | Unit test only (needs a visitor created from the support page) |
| Wording quality, availability wording, tone | **Not verified**: only the real model on staging can show it (`scripts/offerings_eval.py --model real`) |

## 12. UX consequences the FE must surface

- **Cards are snapshots.** Show "as of <time>" when `shown_at` is older than an hour; never refresh a price client-side.
- **A reply can come from the database, not the model** (guard fallback). Show it like any reply; the offer of the team that ends it is real and the next "yes" opens the existing consent flow.
- **The AI will not discount.** If a customer asks for a lower price the answer is the listed price; for negotiable items the owner is told. Do not add your own "make an offer" UI.
- **No quote is not an error.** Quote cards are conditional (section 6); the wanted items are still on the lead.
- **Do not show `guard` or `mode` to customers.** They exist for the audit.
