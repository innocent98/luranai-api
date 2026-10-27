# SOP: Activation backend (knowledge upload fixes, onboarding status, widget-seen tracking)

## What shipped
Branch `feature/activation-onboarding-backend` -> `develop` (PR linked from the hand-back; **not merged**, the lead merges).

1. **Knowledge upload traps fixed**: the text endpoint now really indexes; short documents work; new uploads are chunked per topic; clear failure reasons; failed sources never hold a plan slot.
2. **`GET /api/v1/tenant/onboarding`**: the three-step activation checklist plus "will the AI answer, and if not, why".
3. **Widget-seen tracking** on the public widget config fetch (hostname only, throttled).
4. **WhatsApp consent copy** replaced; `CONSENT_VERSION` = `2026-10-28-draft2`.
5. Found and fixed during the live run: re-uploading identical content crashed (see Findings).

FE contract: `docs/fe-integration-guide-onboarding.md` (every payload copied from `docs/fe-integration-captures/activation/`). Master checklist: "Activation" section.

## Why
Prod aggregates on 2026-10-27: 13 tenants, no payments, 0 of 12 trial tenants ever uploaded a knowledge base, 6 of 10 expired trials never had a conversation. The bottleneck is activation, not tiers. The known causes were all in the path from signup to first answer: a text upload that stored rows but no vectors, short documents rejected with no tenant-facing message, a multi-topic FAQ stored as one chunk that scored under the retrieval cutoff, and no place that tells a tenant "your AI is silent because it is in human-first mode".

## How

### A. Knowledge upload traps (each reproduced with a failing test first)
| Trap | Root cause (proved by test) | Fix |
|---|---|---|
| Text upload stores a source and chunk rows, Qdrant has 0 points | **Three independent defects** in `KnowledgeService.process_document`: (1) point id was `"<source uuid>_<n>"`, which Qdrant rejects (UUID or integer only; verified with the client's local mode, which enforces the same rule); (2) the collection was never ensured on that path; (3) `QdrantManager.upsert_vector` swallows exceptions and returns `False`, and the caller ignored the return, so the log said "Successfully stored 1 chunks" | New `app/services/knowledge_indexing.py::embed_and_store_chunk` is the **one** indexing step for both the text path and the arq worker: UUID point ids (`vector_id` = the point id), `ensure_collection()` first, and a rejected upsert is a failure. The worker had the same unchecked-return flaw (it would have marked a chunk "successful" after a failed upsert); fixed there too |
| Short documents fail ("No chunks could be created") | `chunk_text` dropped the final chunk when it was under `min_chunk_size` (60 tokens), which for a short document is the **only** chunk. It also silently dropped the tail of long documents | Rewritten `chunk_text`: nothing is dropped; any non-empty text yields at least one chunk. Upload rejects below `MIN_DOCUMENT_CHARS = 20` with a clear 422 **before** any source exists |
| ~600-char FAQ is one chunk, scores under 0.35 | One blended chunk dilutes a short question's cosine score | Structure-aware chunking: blank-line blocks; `Q:`/`Question:` lines split per question; a one-line heading or question is glued to its body; pieces under 12 tokens merge with a neighbour; oversize units are packed by line/sentence with overlap as before. **The threshold was not changed**: the eval shows it was not the problem |
| Qdrant collection | Only the worker ensured it | `ensure_collection()` runs before every ingestion and is race-tolerant (a concurrent creator makes "already exists" a success) |
| FAILED / INACTIVE sources eating the cap | They did **not** in `check_knowledge_source_limit` (counts ACTIVE+PROCESSING) or the auditor counters. Two real leaks: a worker crash left the source in PROCESSING forever, and nothing recorded *why* a source failed | Worker marks the in-flight source FAILED on any exception; `failure_code` column + `failure_reason` in API; tests pin FAILED/INACTIVE as non-counting |

**Retrieval eval** (repeatable, real embeddings, ~cents): `scripts/kb_retrieval_eval.py` (loads the old chunker from git via `--before-ref`, default `origin/develop`). Output saved in `docs/fe-integration-captures/activation/kb-retrieval-eval.txt`. Result for a bakery FAQ (hours, delivery, prices, returns), 8 short questions, cutoff 0.35:

| FAQ pasted as | Chunker | Chunks | Questions answered from the right chunk |
|---|---|---|---|
| Sections (393 chars) | before | 1 | **3 / 8** (best scores 0.25-0.42) |
| Sections | after | 4 | **8 / 8** (best scores 0.40-0.55) |
| `Q:`/`A:` lines (476 chars) | before | 1 | **7 / 8** |
| `Q:`/`A:` lines | after | 4 | **8 / 8** (0.41-0.62) |

Limits of the eval: one synthetic FAQ, cosine only (it skips the keyword re-ranker), one embedding model. It shows the direction, not a general quality guarantee.

**Existing KBs are not touched.** Only new ingestions use the new chunker. Original document text is **not stored** (`reprocess_source` is a stub), so a source cannot be re-chunked in place. To re-index: delete the source (`DELETE /api/v1/knowledge/sources/{id}`) and upload the original document again. An ACTIVE source from the old text endpoint has chunk rows but no vectors; the onboarding endpoint reports it as `knowledge_not_indexed`.

**Failure reasons.** `app/utils/knowledge_failures.py`: codes `document_too_short`, `no_text_extracted`, `embedding_failed`, `vector_store_unavailable`, tenant-safe messages, and `KnowledgeError`, rendered by an app-level handler as the flat body `{code, message[, min_chars]}` (422/422/502/503). Stored as `knowledge_sources.failure_code`; jobs derive `failure_reason` from `result_data.failure_code`. Text upload is all-or-nothing: any chunk failing marks the source FAILED, deactivates its chunks and deletes its vectors. The file/URL worker keeps its partial-success rule (>= 1 chunk indexed = ACTIVE) but now deactivates the failed chunks so counts are honest.

### B. Onboarding status and widget-seen
* **`GET /tenant/onboarding`** (`app/services/onboarding_service.py`): `knowledge.done` asks the vector store, not Postgres, using `QdrantManager.count_searchable_vectors(tenant_id, active_source_ids)`, which applies the **same filter as `search()`** (tenant, active, source status ACTIVE, PUBLIC) plus the source ids. If the store cannot be queried it returns `None` and the endpoint reports "not indexed" rather than claiming success it cannot verify (never a 500). Blocker precedence: `no_knowledge` > `knowledge_not_indexed` > `human_first`. Rejected alternative: trusting `chunk_count`/`vector_id` in Postgres, which is exactly what the text-path bug made lie.
* **Widget-seen** (`app/services/widget_seen_service.py`, called from `get_public_widget_config` after the API-key/tenant checks): stores a bare hostname parsed from `Origin` (then `Referer`), validated against a hostname regex; no port, path, query, IP or user agent. Throttle: a cheap check on the already-loaded row, then an `UPDATE ... WHERE last_seen IS NULL OR last_seen <= now-10min`, so concurrent requests cannot double-write. Best effort: any failure is logged and swallowed. Luran's own hosts (`FRONTEND_URL`, `CUSTOMER_FRONTEND_URL`, `FRONTEND_DOC_URL`) plus `WIDGET_SEEN_IGNORED_HOSTS` (comma-separated, `*.suffix` wildcards) are ignored. Rejected alternative: a Redis throttle (extra moving part; the UPDATE's own WHERE is race-safe and needs no TTL bookkeeping).

### C. Consent
`whatsapp_service.CONSENT_TEXT` is now exactly the approved copy; `CONSENT_VERSION = "2026-10-28-draft2"`. No "unofficial" / "not affiliated" / fear wording (test `test_consent_copy_has_no_fear_framing_or_draft_marker`); the old version is rejected (`test_previous_consent_version_is_rejected`). Liability wording belongs in the Terms of Service.

## What's involved
| Area | Files |
|---|---|
| Migration | `alembic/versions/act10_onboarding.py` (`act10onboarding`, down `wa10whatsapp`): `tenants.widget_first_seen_at/widget_last_seen_at/widget_last_seen_host`, `knowledge_sources.failure_code`; all nullable, `SET LOCAL lock_timeout = '5s'` |
| Models/schemas | `app/db/models/tenant.py`, `app/db/models/knowledge.py`, `app/schemas/knowledge.py` (`failure_reason` computed from `failure_code`), `app/schemas/job.py`, `app/schemas/onboarding.py` |
| Ingestion | `app/services/knowledge_service.py` (`process_document`, `create_source`, `fail_source_and_cleanup`, `deactivate_old_versions`), `app/services/knowledge_indexing.py`, `app/workers/document_processor.py`, `app/utils/chunking.py`, `app/utils/qdrant_client.py`, `app/utils/knowledge_failures.py` |
| Endpoints | `app/api/v1/endpoints/knowledge.py`, `knowledge_upload.py`, `tenant.py` (`/onboarding`), `widget.py`, `app/main.py` (handler) |
| Onboarding/widget | `app/services/onboarding_service.py`, `app/services/widget_seen_service.py`, `app/core/config.py` (`WIDGET_SEEN_IGNORED_HOSTS`) |
| Consent | `app/services/whatsapp_service.py` |
| Tests | `tests/knowledge/`, `tests/onboarding/`, `tests/migrations/test_act10_onboarding.py`; edited `tests/whatsapp/test_connections_api.py`, `tests/migrations/test_wa10_whatsapp_channel.py` (it assumed wa10 was the head: now downgrades to wa10's parent), `tests/auditor/test_service_import.py` (its mock returned `None` from `upsert_vector`; a failed upsert now fails, so it returns `True`) |
| Tooling/evidence | `scripts/kb_retrieval_eval.py`, `scripts/capture_activation.py`, `docs/fe-integration-captures/activation/` |

## Verification
* **CI parity, local, 2026-10-27**: ruff 0.8.6 `check app/` clean; black 24.10.0 `--check app/` clean; `pytest tests/` full suite (see Final numbers below); SQLite regression job (`test_chat_flow_router`, `test_flow_action_call_regressions`) 14 passed. Python 3.11 venv via poetry (CI's version).
* **Migration**: `alembic heads` = one head `act10onboarding`; on a scratch Postgres 15 DB `upgrade head` -> columns exist and nullable -> `downgrade -1` (all four columns gone, back at `wa10whatsapp`) -> `upgrade head` again.
* **Live end to end** (local Postgres 15, Qdrant 1.16.2, the real arq worker, real OpenAI embeddings and chat model; 36 captures): text upload indexes (4 chunks for the FAQ), short doc works, 422s create no source, file job failure carries `failure_reason`, Qdrant stopped -> 503 + FAILED source + onboarding stays honest, invalid OpenAI key -> 502, widget host-only + dashboard origin ignored + throttle, a real chat answered from the uploaded FAQ ("We open at 7am Monday through Saturday..."), tenant B sees only zeros, B cannot upload into A.
* **Not proven live**: widget update after the 10-minute window (unit tests only); the old-consent-version 422; WebSocket `failure_reason`; URL uploads; real scanned PDFs; `embedding_failed` / `vector_store_unavailable` as *job* failures (unit tests only); behaviour on staging/prod; any load or concurrency test; the dashboard UI.
* **Opus review** of the security-relevant parts: see "Security review" below.

## Findings during the live run
1. **Re-uploading identical content crashed** (fixed, `f917ed8`): `create_source` computed the next version from non-inactive rows and read them with `scalar_one_or_none()`, so the third upload of the same text (a retry after failures, the activation path itself) raised `MultipleResultsFound` (500), and re-uploading after a delete hit `uq_knowledge_source_tenant_hash_version`. Versions now continue from the highest prior row of any status. The worker path now retires the previous version (it never did, leaving two ACTIVE copies), and retired versions' vectors are deleted so they stop taking retrieval slots.
2. My first scrubber redacted the step `key` field in captures (harness bug, fixed) and a stale worker from before a fix was still consuming jobs (`pkill` pattern missed the macOS `Python` process). Both would have produced misleading evidence; the final captures are from a single current worker.
3. Model/DB drift left alone: `knowledge_chunks.vector_id` is nullable in the database (migration `445f25417efa`) but `nullable=False` in the model. The ingestion paths now always write a UUID, so it does not matter, but the model should be corrected in its own change.

## Operate / roll back
* **Deploy**: run `alembic upgrade head` (adds four nullable columns, 5 s lock timeout, no rewrite). Worker and API both need the new code (the worker writes `failure_code` and calls the shared indexing step).
* **Config**: optional `WIDGET_SEEN_IGNORED_HOSTS=preview.example.com,*.vercel-preview.example`. Add every host that serves the dashboard or a widget preview, or opening it will tick the "widget seen" step.
* **Roll back**: revert the PR and `alembic downgrade wa10whatsapp` (drops the four columns, losing widget-seen data and recorded failure reasons; nothing else depends on them). Already-created chunks/vectors are valid either way.
* **Re-index a source**: delete it, upload the original again (see A).

## Follow-ups
* One-off repair for legacy sources: ACTIVE with chunk rows but no vectors (old text endpoint). Chunk text is in Postgres, so vectors could be rebuilt from `knowledge_chunks.content` without the original file. Worth doing only if any such prod rows exist (check with the onboarding query or a Qdrant count by source).
* `POST /knowledge/upload` (text) never enforced the plan cap or storage limit; the file/URL endpoints do. Decide whether it should (pricing/entitlement decision, out of scope here).
* A source stuck in PROCESSING by a hard-killed worker (SIGKILL, host crash) still counts toward the cap; add a reaper for PROCESSING older than N minutes.
* The text endpoint embeds synchronously inside the request; a very large paste could hit proxy timeouts. Consider routing it through the job queue like files.
* Fix the `KnowledgeChunk.vector_id` nullability drift in the model.
* Make the deactivated-version vector cleanup and the cap check for the text endpoint part of a single "replace document" flow if the dashboard adds one.

## Security review
See the section appended below after the Opus review.
