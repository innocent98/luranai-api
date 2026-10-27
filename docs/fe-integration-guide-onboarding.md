# FE integration guide: onboarding / activation (get a new tenant to first value)

Backend track: `feature/activation-onboarding-backend`. Dashboard track builds against the contract in
`docs/specs/2026-10-28-activation-contract.md` (lead's repo); this guide is the **verified** version of it.

**Where the numbers come from.** Every request/response below is copied verbatim from files in
`docs/fe-integration-captures/activation/` (named `NN-scenario.json`), recorded on 2026-10-27 by
`scripts/capture_activation.py` against a locally running app: Postgres 15, Qdrant 1.16.2, the real arq
worker, real OpenAI embeddings and chat model. IDs, names and timestamps come from a throwaway local database
(placeholders, not real people). Tokens and API keys are replaced by `<...-placeholder>`. Anything that could
**not** be exercised live is marked **[NOT LIVE]** inline and listed in the verification table at the end.

Base URL below is `/api/v1`. All `/tenant/*` and `/knowledge/*` routes need `Authorization: Bearer <tenant-admin jwt>`.

---

## 1. What this is, in the FE's terms

A new tenant must reach three things before Luran is useful:

1. **Knowledge**: at least one document the AI can really search (indexed, not just uploaded).
2. **Widget**: the chat widget has loaded on one of their websites.
3. **Test conversation**: one real customer message has been sent and answered.

Plus one banner that answers "**will the AI actually answer a customer right now, and if not, why?**"
(`ai.will_answer` / `ai.blocker`). `GET /tenant/onboarding` returns all of it in one call.

---

## 2. `GET /tenant/onboarding`

Auth: tenant admin (same as the other `/tenant/*` routes). Scoped to the caller's own tenant; there is no tenant id
in the URL and none can be supplied.

### 2.1 Brand-new tenant (capture `01-onboarding-fresh`)

Response **200**:
```json
{
  "steps": [
    {
      "key": "knowledge",
      "done": false,
      "detail": {
        "sources_active": 0,
        "sources_processing": 0,
        "sources_failed": 0
      }
    },
    {
      "key": "widget",
      "done": false,
      "detail": {
        "first_seen_at": null,
        "last_seen_at": null,
        "last_seen_host": null
      }
    },
    {
      "key": "test_conversation",
      "done": false,
      "detail": {
        "conversations": 0
      }
    }
  ],
  "ai": {
    "first_responder": "human",
    "will_answer": false,
    "blocker": "no_knowledge"
  },
  "completed": 0,
  "total": 3
}
```

### 2.2 Fields

| Field | Type | Meaning |
|---|---|---|
| `steps` | array of 3, always in the order knowledge, widget, test_conversation | **Look steps up by `key`**, not by index. |
| `steps[].key` | `"knowledge"` \| `"widget"` \| `"test_conversation"` | |
| `steps[].done` | bool | The checklist tick. |
| `steps[knowledge].detail` | `{sources_active, sources_processing, sources_failed}` ints | Counts of the tenant's sources by status (inactive/deleted are not counted). |
| `steps[widget].detail` | `{first_seen_at, last_seen_at, last_seen_host}` | ISO-8601 UTC with a `Z` suffix, or `null`. Host is a bare hostname. |
| `steps[test_conversation].detail` | `{conversations}` int | Conversations with at least one customer message. |
| `ai.first_responder` | `"ai"` \| `"ai_with_handoff"` \| `"human"` | Same value as `GET /tenant/ai-config`. |
| `ai.will_answer` | bool | `true` only when `blocker` is `null`. |
| `ai.blocker` | `null` \| `"no_knowledge"` \| `"knowledge_not_indexed"` \| `"human_first"` | Why the AI will stay silent. |
| `completed` / `total` | int | Done steps out of 3. |

**`knowledge.done` is stricter than "a source exists".** It is true only when at least one ACTIVE source has vectors
the AI can really retrieve. A source can be `active` in the list yet not indexed (rows uploaded through the old
text endpoint); the status call is the only place that tells the truth.

**`blocker` precedence** (first match wins): `no_knowledge` (no ACTIVE source at all, even if one is still
processing) -> `knowledge_not_indexed` (ACTIVE sources but nothing searchable) -> `human_first` (indexed, but
`first_responder` is `human`).

### 2.3 Examples (all real)

Knowledge indexed, tenant still in human-first mode (capture `07-onboarding-knowledge-done-human-first`):
```json
{
  "steps": [
    {
      "key": "knowledge",
      "done": true,
      "detail": {
        "sources_active": 1,
        "sources_processing": 0,
        "sources_failed": 0
      }
    },
    {
      "key": "widget",
      "done": false,
      "detail": {
        "first_seen_at": null,
        "last_seen_at": null,
        "last_seen_host": null
      }
    },
    {
      "key": "test_conversation",
      "done": false,
      "detail": {
        "conversations": 0
      }
    }
  ],
  "ai": {
    "first_responder": "human",
    "will_answer": false,
    "blocker": "human_first"
  },
  "completed": 1,
  "total": 3
}
```

After "Let AI answer first" (capture `09-onboarding-ai-will-answer`): `blocker` is `null`.
```json
{
  "steps": [
    {
      "key": "knowledge",
      "done": true,
      "detail": {
        "sources_active": 1,
        "sources_processing": 0,
        "sources_failed": 0
      }
    },
    {
      "key": "widget",
      "done": false,
      "detail": {
        "first_seen_at": null,
        "last_seen_at": null,
        "last_seen_host": null
      }
    },
    {
      "key": "test_conversation",
      "done": false,
      "detail": {
        "conversations": 0
      }
    }
  ],
  "ai": {
    "first_responder": "ai_with_handoff",
    "will_answer": true,
    "blocker": null
  },
  "completed": 1,
  "total": 3
}
```

Widget seen (capture `21-onboarding-widget-seen`). Note `last_seen_host` is a hostname only, although the request had a port, a path and a query:
```json
{
  "steps": [
    {
      "key": "knowledge",
      "done": true,
      "detail": {
        "sources_active": 2,
        "sources_processing": 0,
        "sources_failed": 0
      }
    },
    {
      "key": "widget",
      "done": true,
      "detail": {
        "first_seen_at": "2026-10-27T22:30:48.680287Z",
        "last_seen_at": "2026-10-27T22:30:48.680287Z",
        "last_seen_host": "demo-bakery.example.com"
      }
    },
    {
      "key": "test_conversation",
      "done": false,
      "detail": {
        "conversations": 0
      }
    }
  ],
  "ai": {
    "first_responder": "ai_with_handoff",
    "will_answer": true,
    "blocker": null
  },
  "completed": 2,
  "total": 3
}
```

All three done (capture `25-onboarding-all-done`):
```json
{
  "steps": [
    {
      "key": "knowledge",
      "done": true,
      "detail": {
        "sources_active": 2,
        "sources_processing": 0,
        "sources_failed": 0
      }
    },
    {
      "key": "widget",
      "done": true,
      "detail": {
        "first_seen_at": "2026-10-27T22:30:48.680287Z",
        "last_seen_at": "2026-10-27T22:30:48.680287Z",
        "last_seen_host": "demo-bakery.example.com"
      }
    },
    {
      "key": "test_conversation",
      "done": true,
      "detail": {
        "conversations": 1
      }
    }
  ],
  "ai": {
    "first_responder": "ai_with_handoff",
    "will_answer": true,
    "blocker": null
  },
  "completed": 3,
  "total": 3
}
```

An `active` source with **no** vectors (capture `29-onboarding-active-source-without-vectors`; **the row was inserted directly in the DB to simulate a pre-fix upload**, no endpoint can create one any more). `sources_active` is 1 but `done` is false:
```json
{
  "steps": [
    {
      "key": "knowledge",
      "done": false,
      "detail": {
        "sources_active": 1,
        "sources_processing": 0,
        "sources_failed": 0
      }
    },
    {
      "key": "widget",
      "done": false,
      "detail": {
        "first_seen_at": null,
        "last_seen_at": null,
        "last_seen_host": null
      }
    },
    {
      "key": "test_conversation",
      "done": false,
      "detail": {
        "conversations": 0
      }
    }
  ],
  "ai": {
    "first_responder": "human",
    "will_answer": false,
    "blocker": "knowledge_not_indexed"
  },
  "completed": 0,
  "total": 3
}
```

Vector store down while the tenant has indexed knowledge (capture `32-onboarding-vector-store-down`; Qdrant container stopped on purpose). The call still returns 200 and does **not** claim success it cannot verify, so the banner reads `knowledge_not_indexed` until the store is back:
```json
{
  "steps": [
    {
      "key": "knowledge",
      "done": false,
      "detail": {
        "sources_active": 2,
        "sources_processing": 0,
        "sources_failed": 1
      }
    },
    {
      "key": "widget",
      "done": true,
      "detail": {
        "first_seen_at": "2026-10-27T22:30:48.680287Z",
        "last_seen_at": "2026-10-27T22:30:48.680287Z",
        "last_seen_host": "demo-bakery.example.com"
      }
    },
    {
      "key": "test_conversation",
      "done": true,
      "detail": {
        "conversations": 1
      }
    }
  ],
  "ai": {
    "first_responder": "ai_with_handoff",
    "will_answer": false,
    "blocker": "knowledge_not_indexed"
  },
  "completed": 2,
  "total": 3
}
```

### 2.4 Errors

| Status | Body (verbatim) | Capture |
|---|---|---|
| 401 | `{"detail": "Could not validate credentials"}` | `28-onboarding-no-auth` |

Tenant isolation, live (capture `26-onboarding-other-tenant-sees-only-itself`): a second tenant, queried while tenant A had full activity, sees only zeros:
```json
{
  "steps": [
    {
      "key": "knowledge",
      "done": false,
      "detail": {
        "sources_active": 0,
        "sources_processing": 0,
        "sources_failed": 0
      }
    },
    {
      "key": "widget",
      "done": false,
      "detail": {
        "first_seen_at": null,
        "last_seen_at": null,
        "last_seen_host": null
      }
    },
    {
      "key": "test_conversation",
      "done": false,
      "detail": {
        "conversations": 0
      }
    }
  ],
  "ai": {
    "first_responder": "human",
    "will_answer": false,
    "blocker": "no_knowledge"
  },
  "completed": 0,
  "total": 3
}
```

### 2.5 Polling

* Call it when the onboarding screen opens and after any action that changes it (upload finished, AI mode saved).
* While a knowledge job is running, poll **no faster than every 3 s**; stop when `sources_processing` is 0 or the tab is hidden.
* Widget and conversation steps change on the customer's schedule: refresh on focus or every 30-60 s while the screen is open. `widget.last_seen_at` is **throttled to one write per 10 minutes** (section 5), so it can look up to 10 minutes old.
* The call does one vector-store lookup; do not poll it in a tight loop or on every page of the dashboard.

### 2.6 What to show for each blocker (instructions, not suggestions)

| `ai.blocker` | Tell the tenant | Action |
|---|---|---|
| `no_knowledge` | "Your AI has nothing to answer from yet." If `sources_processing > 0`, say it is still being prepared. If `sources_failed > 0`, link to the failed source and show its `failure_reason.message`. | Upload knowledge. |
| `knowledge_not_indexed` | "Your documents are saved but the AI cannot search them yet." This also appears during a short vector-store outage, so offer "Try again in a few minutes" before "Re-upload". | Re-upload the document. |
| `human_first` | "Your AI is in human-first mode, so it will not answer customers." **Never leave this silent**: the tenant otherwise sees a working setup and a silent bot. | Offer **Let AI answer first** (section 2.7). |
| `null` | "Your AI will answer customers." | none |

### 2.7 "Let AI answer first" (contract section 4, no new endpoint)

Reuse the existing AI-config update (capture `08-ai-config-let-ai-answer-first`). Request:
```json
{
  "first_responder": "ai_with_handoff"
}
```
Response **200** (full AI config; note `no_retrieval_action` / `low_confidence_action` become `"escalate"`: this mode hands unknown questions to a human instead of guessing):
```json
{
  "brand_voice": {},
  "allowed_topics": null,
  "blocked_topics": null,
  "escalation_keywords": [],
  "max_response_tokens": 450,
  "grounding_requirement": true,
  "grounding_mode": "balanced",
  "low_confidence_action": "escalate",
  "no_retrieval_action": "escalate",
  "first_responder": "ai_with_handoff",
  "require_email_before_escalation": false,
  "require_name_before_escalation": false,
  "collect_name_before_chat": false,
  "collect_email_before_chat": false,
  "custom_instructions": null,
  "id": "a9587608-8d13-450b-8bd1-8d2fc5abec75",
  "tenant_id": "3c60c339-7a7b-4c34-966b-a108c6e3694b",
  "active_knowledge_sources": 1,
  "created_at": "2026-10-27T22:30:36.630363Z",
  "updated_at": "2026-10-27T22:30:39.700425Z"
}
```
Then refetch `/tenant/onboarding`: `ai.first_responder` is `"ai_with_handoff"` and the blocker is gone.

---

## 3. Knowledge uploads: new errors, `failure_reason`

### 3.1 The one error shape to handle

Upload failures use a **flat** body, `{ "code": "...", "message": "..." }` (plus `min_chars` for one code). This is **different** from most other errors in this API, which use `{ "detail": ... }` (see the 403 and 401 examples in this guide). Handle both: if the body has a string `code`, show `message`.

`message` is written for tenants and is safe to display as-is.

| HTTP | `code` | When | Source row created? |
|---|---|---|---|
| 422 | `document_too_short` | Text upload under 20 characters (after trimming) | **No** |
| 422 | `no_text_extracted` | Text with no letters or digits; or a file with nothing readable (e.g. scanned PDF) | **No** |
| 502 | `embedding_failed` | Text upload; the embeddings provider failed | Yes, as `failed` |
| 503 | `vector_store_unavailable` | Text upload; the vector store was unreachable or rejected the write | Yes, as `failed` |

For **file/URL** uploads (queued jobs) the same codes arrive on the **job** (3.3) except `no_text_extracted` for an unreadable file, which is rejected immediately with the 422 above because extraction happens in the request.

### 3.2 Text upload `POST /knowledge/upload`

Too short (capture `02-text-upload-too-short`, request `{"document_text": "hello", ...}`). Response **422**:
```json
{
  "code": "document_too_short",
  "message": "This document is too short. Add at least 20 characters of text so the AI has something to answer from.",
  "min_chars": 20
}
```
Empty/whitespace gives the identical body (capture `03-text-upload-empty`). No letters (capture `04-text-upload-no-letters`), **422**:
```json
{
  "code": "no_text_extracted",
  "message": "We could not find any readable text in this document. If it is a scanned PDF or an image, upload a text version or paste the text instead."
}
```
The rejected uploads created nothing (capture `05-sources-after-rejected-uploads`): `[]`

A 25-character document now **works** (it used to fail silently); one chunk, indexed (capture `06-text-upload-short-but-valid`), **201**:
```json
{
  "name": "hours-one-liner",
  "source_type": "faq",
  "meta": {},
  "id": "3285002f-71fd-481b-8947-5e639c13ac2c",
  "tenant_id": "3c60c339-7a7b-4c34-966b-a108c6e3694b",
  "file_url": null,
  "content_hash": "9d95f3e05a122944e96c3f958f82a0b9f2a042578543e8ff22ec9b71826fed73",
  "version": 1,
  "status": "active",
  "chunk_count": 1,
  "uploaded_by": "a6e88c8d-0a1f-4e33-b9a6-015271a404f5",
  "source_category": "file",
  "source_url": null,
  "file_size_bytes": null,
  "created_at": "2026-10-27T22:30:39.002584Z",
  "updated_at": "2026-10-27T22:30:39.665830Z",
  "failure_reason": null
}
```

A multi-topic FAQ is split per topic (capture `10-text-upload-faq`, `chunk_count` 4; chunks in capture `12-source-chunks`). Request:
```json
{
  "tenant_id": "3c60c339-7a7b-4c34-966b-a108c6e3694b",
  "document_name": "bakery-faq",
  "document_text": "Opening hours\nWe are open Monday to Saturday from 7am to 6pm, and on Sundays from 8am to 2pm.\n\nDelivery\nWe deliver within 10km of the shop. Delivery costs 1,500 naira and takes about 45 minutes.\n\nPrices\nA loaf of bread is 1,200 naira. Birthday cakes start at 15,000 naira and need two days' notice.\n\nReturns\nIf your order is wrong, tell us within 24 hours and we will replace it or refund you.\n",
  "source_type": "faq"
}
```
Response **201** (`failure_reason` is `null` on success):
```json
{
  "name": "bakery-faq",
  "source_type": "faq",
  "meta": {},
  "id": "7c07e66a-a7b4-4ab4-a968-a44108aa7f0e",
  "tenant_id": "3c60c339-7a7b-4c34-966b-a108c6e3694b",
  "file_url": null,
  "content_hash": "a88ffdfc6497e65c73a00b4e7815d57a1a60b3e993093915082e1f61317bb39e",
  "version": 1,
  "status": "active",
  "chunk_count": 4,
  "uploaded_by": "a6e88c8d-0a1f-4e33-b9a6-015271a404f5",
  "source_category": "file",
  "source_url": null,
  "file_size_bytes": null,
  "created_at": "2026-10-27T22:30:39.730708Z",
  "updated_at": "2026-10-27T22:30:41.541211Z",
  "failure_reason": null
}
```
Chunks (capture `12-source-chunks`, list of 4, first shown):
```json
{
  "content": "Opening hours\nWe are open Monday to Saturday from 7am to 6pm, and on Sundays from 8am to 2pm.",
  "meta": {
    "token_count": 30,
    "source_name": "bakery-faq",
    "source_type": "faq"
  },
  "visibility": "public",
  "language": "en",
  "id": "15d2a9e5-efb3-4ad3-b2b2-0c7a78f153fa",
  "tenant_id": "3c60c339-7a7b-4c34-966b-a108c6e3694b",
  "source_id": "7c07e66a-a7b4-4ab4-a968-a44108aa7f0e",
  "chunk_index": 0,
  "vector_id": "decbe5d6-10a1-451a-9e5e-eeaf94bc4719",
  "version": 1,
  "is_active": true,
  "created_at": "2026-10-27T22:30:39.735147Z",
  "updated_at": "2026-10-27T22:30:39.735147Z"
}
```

Failures that leave a `failed` source:

Vector store down (capture `30-text-upload-vector-store-down`), **503**:
```json
{
  "code": "vector_store_unavailable",
  "message": "We could not save this document to the AI's knowledge store right now. Please try uploading it again in a few minutes."
}
```
Embedding provider failing (capture `33-text-upload-embedding-failed`; the second API instance had an invalid OpenAI key), **502**:
```json
{
  "code": "embedding_failed",
  "message": "We could not prepare this document for the AI right now. Please try uploading it again in a few minutes."
}
```

### 3.3 File upload `POST /knowledge/upload-file` and the job

The request is unchanged (multipart). A readable but too-short file is accepted (capture `13-file-upload-too-short`), **202**:
```json
{
  "job_id": "935b22d4-f4a8-49db-a20f-e35107f42565",
  "status": "queued",
  "message": "Document upload queued for processing. Check status via provided URL or connect to WebSocket.",
  "check_status_url": "/api/v1/jobs/935b22d4-f4a8-49db-a20f-e35107f42565",
  "estimated_chunks": 1,
  "estimated_time_seconds": 6
}
```
Poll `GET /jobs/{job_id}`. The job now carries `failure_reason`, and **no source is created** (capture `14-job-failed-document-too-short`):
```json
{
  "job_type": "document_ingestion",
  "total_items": null,
  "id": "935b22d4-f4a8-49db-a20f-e35107f42565",
  "tenant_id": "3c60c339-7a7b-4c34-966b-a108c6e3694b",
  "status": "failed",
  "processed_items": 0,
  "failed_items": 0,
  "progress_percent": 0,
  "result_data": {
    "failure_code": "document_too_short"
  },
  "error_message": "This document is too short. Add at least 20 characters of text so the AI has something to answer from.",
  "created_at": "2026-10-27T22:30:41.582215",
  "started_at": "2026-10-27T22:30:41.817468",
  "completed_at": "2026-10-27T22:30:41.828906",
  "created_by": "a6e88c8d-0a1f-4e33-b9a6-015271a404f5",
  "failure_reason": {
    "code": "document_too_short",
    "message": "This document is too short. Add at least 20 characters of text so the AI has something to answer from."
  }
}
```
A good file (capture `16-job-completed`): `failure_reason` is `null`.
```json
{
  "job_type": "document_ingestion",
  "total_items": 4,
  "id": "e80145b5-2e38-41b7-90ab-420d376cee23",
  "tenant_id": "3c60c339-7a7b-4c34-966b-a108c6e3694b",
  "status": "completed",
  "processed_items": 4,
  "failed_items": 0,
  "progress_percent": 100,
  "result_data": {
    "source_id": "be8de611-9392-408a-bf74-d4beccea51df",
    "source_name": "menu.txt",
    "chunks_created": 4,
    "chunks_failed": 0
  },
  "error_message": null,
  "created_at": "2026-10-27T22:30:42.636355",
  "started_at": "2026-10-27T22:30:42.816149",
  "completed_at": "2026-10-27T22:30:47.690461",
  "created_by": "a6e88c8d-0a1f-4e33-b9a6-015271a404f5",
  "failure_reason": null
}
```
A file with nothing readable is rejected at once, **422** (capture `17-file-upload-no-text`):
```json
{
  "code": "no_text_extracted",
  "message": "We could not find any readable text in this document. If it is a scanned PDF or an image, upload a text version or paste the text instead."
}
```
The job's WebSocket final message also gained a `failure_reason` field. **[NOT LIVE]**: the WebSocket was not exercised; the field is built by the same code as the REST value but only the REST shape is captured. Use the REST job as the source of truth.

### 3.4 `failure_reason` on sources

Every source in `GET /knowledge/tenants/{tenant_id}/sources`, `GET /knowledge/sources/{id}` and the 201 above has
`failure_reason`: `null` or `{code, message}`. A **failed** source is listed (status `failed`) so the tenant can see
what went wrong (capture `31-sources-list-with-failed`; list, the failed entry is first):
```json
{
  "name": "during-outage",
  "source_type": "help_doc",
  "meta": {},
  "id": "9d3f87f1-c173-4d1b-9491-339a6bafdcd6",
  "tenant_id": "3c60c339-7a7b-4c34-966b-a108c6e3694b",
  "file_url": null,
  "content_hash": "a88ffdfc6497e65c73a00b4e7815d57a1a60b3e993093915082e1f61317bb39e",
  "version": 3,
  "status": "failed",
  "chunk_count": 0,
  "uploaded_by": "a6e88c8d-0a1f-4e33-b9a6-015271a404f5",
  "source_category": "file",
  "source_url": null,
  "file_size_bytes": null,
  "created_at": "2026-10-27T22:31:01.765465Z",
  "updated_at": "2026-10-27T22:31:01.777647Z",
  "failure_reason": {
    "code": "vector_store_unavailable",
    "message": "We could not save this document to the AI's knowledge store right now. Please try uploading it again in a few minutes."
  }
}
```
Embedding failure shows the same way (capture `34-sources-list-embedding-failed`, `failure_reason.code` `embedding_failed`). `GET /knowledge/sources/{id}` returns the same schema family and so carries the same field; it was not captured separately **[NOT LIVE]**.
Show `failure_reason.message` next to a delete/retry action. Retrying means uploading the same document again; it now works any number of times.

### 3.5 Plan cap

`failed` and `inactive` sources do **not** count toward `max_knowledge_sources`. Capture `35-file-upload-at-cap`: the tenant had two live sources and two `failed` ones with a cap of 2; the count in the refusal is `2/2` (live only). **403**, note the nested `detail` object (a different shape from the flat errors in 3.1):
```json
{
  "detail": {
    "error": "knowledge_limit_reached",
    "message": "Knowledge source limit reached (2/2).",
    "current_count": 2,
    "limit": 2,
    "upgrade_message": "Upgrade to Professional for 30 sources or Enterprise for unlimited."
  }
}
```
**Not changed:** `POST /knowledge/upload` (text) does not call the plan-cap check (confirmed by reading the endpoint; I did not probe it live with a full tenant); only the file and URL endpoints do. Do not rely on the text endpoint to enforce limits.

### 3.6 Behaviour changes the FE will notice

* Short documents (20+ characters) are now accepted and answer questions.
* New uploads are split **per topic** (heading + body, or Q&A pair, or paragraph), so the chunk count for the same text is higher than before. Existing sources are untouched.
* Uploading the **same content again** replaces the older copy: the older one becomes `inactive` (hidden from the default list), the new one is the only active copy. It does not consume an extra slot.

---

## 4. Widget seen (the `widget` step)

Nothing for the FE to call: it happens when the embedded widget loads its config,
`GET /widget/public/config/{tenant_id}` with `X-API-Key: <public key>`. The response body of that call is **unchanged**.

* Stored on the tenant: first seen, last seen, and the **hostname only** of the page (taken from the browser's `Origin`, falling back to `Referer`). The port, path and query are dropped (capture `20-widget-config-from-customer-site` sent `Origin: https://demo-bakery.example.com:8443` and a Referer with a path and query; the stored host in `21-onboarding-widget-seen` is `demo-bakery.example.com`).
* Luran's own origins (the dashboard, preview, docs hosts) are **ignored**: capture `18-widget-config-from-dashboard-origin` then `19-onboarding-widget-not-seen` (`done: false`). The ignored hosts are the hosts of the backend's `FRONTEND_URL`, `CUSTOMER_FRONTEND_URL` and `FRONTEND_DOC_URL` settings plus the comma-separated `WIDGET_SEEN_IGNORED_HOSTS` list (`*.example.com` wildcards allowed). **Ops/dashboard must add any other host that serves the dashboard or a widget preview to `WIDGET_SEEN_IGNORED_HOSTS`**, otherwise opening it will tick this step. Which hosts the dashboard preview uses was not checked from this repo.
* **Throttled**: at most one write per tenant per 10 minutes. Capture `22-widget-config-second-site-within-10-min` fetched from a second site; `23-onboarding-widget-throttled` shows `last_seen_at` and `last_seen_host` unchanged. `first_seen_at` never changes after it is set.
* A request with no `Origin` and no `Referer` (a script, not a browser) records nothing.
* Re-firing after the 10-minute window updates `last_seen_*`: **[NOT LIVE]**, covered by unit tests only (`tests/onboarding/test_widget_seen.py`); waiting 10 minutes was not done in the live run.

UX instructions:
* Show it as "Seen on `<last_seen_host>` <relative time of last_seen_at>". Never print it as proof the widget is installed on the **right** site, only that it loaded somewhere.
* `done: false` means "not seen yet", not "broken". The browser may simply not have loaded the page yet; offer a "Check again" that just refetches.
* Hostnames like `localhost` count (a developer testing locally); show them as-is.
* Render `last_seen_host` as plain text (it is a browser-supplied header, validated to a hostname, but treat it as untrusted input like any other).

---

## 5. Test conversation (the `test_conversation` step)

Done when a conversation has at least one **customer** message, from the widget, a hosted page, or WhatsApp. Capture `24-chat-first-message` is the real call (`POST /chat/message`, no auth); the reply came from the uploaded FAQ:
```json
{
  "session_id": "71327e7e-1315-4c26-912f-eefb8e348557",
  "message": {
    "role": "assistant",
    "content": "We open at 7am Monday through Saturday, and at 8am on Sundays.",
    "id": "9f723fa4-eabf-4475-bb30-0a61dbd36d52",
    "session_id": "71327e7e-1315-4c26-912f-eefb8e348557",
    "retrieved_chunk_ids": [
      "1865a465-ea09-455c-85f7-5a750198f92a",
      "54208bd9-8ce3-4ef2-a187-5c7a0fa158b6"
    ],
    "token_count": 544,
    "attachments": [],
    "reply_to": null,
    "meta": {
      "chunks_used": 2,
      "avg_similarity": 0.494,
      "retrieval_hit": true,
      "escalation_action": "no_escalation",
      "response_quality": {
        "is_substantial": true,
        "has_uncertainty": false,
        "word_count": 12,
        "char_count": 62
      },
      "token_breakdown": {
        "prompt_tokens": 194,
        "completion_tokens": 350,
        "total_tokens": 544
      }
    },
    "created_at": "2026-10-27T22:31:00.423875Z"
  },
  "user_message_attachments": [],
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```
Request was `{"message": "What time do you open?", "tenant_id": "3c60c339-7a7b-4c34-966b-a108c6e3694b", "user_identifier": "visitor-demo-001"}`. Afterwards `25-onboarding-all-done` shows `conversations: 1`.

---

## 6. WhatsApp consent copy (replaces the draft)

`GET /whatsapp/consent` (capture `36-whatsapp-consent`), **200**:
```json
{
  "version": "2026-10-28-draft2",
  "text": "Luran connects your WhatsApp number as a linked device so it can answer customers who message you. WhatsApp can change its policies, restrict access, or disconnect the linked device. Luran replies only to customers who message you first. You can keep using WhatsApp on your phone or WhatsApp Web at the same time: when you type a reply yourself, the AI pauses on that chat for a while. You can disconnect at any time."
}
```
Show `text` **verbatim** and send back the `version` when creating a connection. The previous version (`2026-10-27-draft1`) is now rejected with 422 `consent_required`; the message is built from the current version string: `Accept the current WhatsApp terms (version 2026-10-28-draft2) to continue.` **[NOT LIVE]**: that 422 was not re-captured against the running app (it needs the sidecar gating); it is asserted by `tests/whatsapp/test_connections_api.py::test_previous_consent_version_is_rejected`. The earlier captures in `docs/fe-integration-captures/whatsapp-channel/` still show `2026-10-27-draft1` and the old text because they were recorded before this change.

Product rule: no "unofficial", "not affiliated" or fear wording on this screen; liability wording belongs in the Terms of Service.

---

## 7. Traps (read before wiring)

1. **Two error shapes.** Upload failures are flat `{code, message}`; most other failures are `{detail: ...}` with `detail` sometimes an object (cap 403). Branch on the presence of a string `code`.
2. **`done` is not "exists".** `knowledge.done` requires real vectors; `sources_active` can be > 0 while `done` is false.
3. **`first_responder` appears in three places** at different levels: `GET /tenant/onboarding` -> `ai.first_responder`, `GET/PUT /tenant/ai-config` -> top level `first_responder`, `GET /widget/public/config/{id}` -> top level `first_responder`. Same value, different nesting.
4. **`key` is the step id**, but the API key header and other `key` fields elsewhere are unrelated.
5. **Datetimes**: onboarding uses `Z`-suffixed ISO strings; job fields (`created_at`, `started_at`, `completed_at`) have **no** timezone suffix (see capture 14/16). Treat job times as UTC.
6. **`failure_reason` exists on jobs and sources**, but `result_data.failure_code` on a failed job is the same code in raw form; use `failure_reason`.
7. **Failed sources stay in the list** (status `failed`); they are tombstones with an explanation, not errors to hide.

---

## 8. Verification table

| Behaviour | Status |
|---|---|
| `GET /tenant/onboarding` shape, fresh tenant, blockers `no_knowledge`, `human_first`, `knowledge_not_indexed`, `null` | verified live (captures 01, 07, 09, 29) |
| Vector-store-down behaviour of onboarding | verified live (32) |
| Tenant isolation of onboarding (B sees only zeros while A is active; B uploading into A is 403; no auth is 401) | verified live (26, 27, 28) |
| `knowledge.done` ignores another tenant's vectors | unit test only (`test_other_tenants_vectors_do_not_count`) |
| Widget seen: records host only, ignores dashboard origin, throttles inside 10 min | verified live (18-23) |
| Widget seen: update after the 10-minute window; wildcard ignore entries; no-Origin request | unit tests only |
| Text upload 422s (`document_too_short`, `no_text_extracted`), nothing created | verified live (02-05) |
| Short (25 char) document indexed; FAQ split per topic | verified live (06, 10, 12) |
| 503 `vector_store_unavailable`, 502 `embedding_failed`, `failed` source with `failure_reason` | verified live (30, 31, 33, 34) |
| File upload: too-short job failure with `failure_reason`, completed job, unreadable file 422 | verified live (13, 14, 16, 17) |
| `embedding_failed` / `vector_store_unavailable` as a *job* failure (file/URL path) | unit tests only (`tests/knowledge/test_worker_ingestion.py`); the live run captured them on the text path |
| Job `failure_reason` over WebSocket | NOT live |
| URL upload (`/knowledge/upload-url`) failure codes | NOT live (same worker code as files) |
| Real scanned PDF / DOCX extraction edge cases | NOT live (a symbols-only `.txt` stood in) |
| Failed sources not counted toward the cap | verified live (35) + unit tests |
| Re-upload of identical content replaces the old copy (file and text paths) | verified live in the DB during the run (old version `inactive`) + unit tests; not a separate capture |
| Real chat answered from uploaded FAQ; conversation step | verified live (24, 25) |
| WhatsApp consent text/version | verified live (36); old-version 422 NOT live (unit test) |
| Retrieval quality before/after chunker | measured with real embeddings: `kb-retrieval-eval.txt` |
