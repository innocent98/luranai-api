# Voice Chat — De-risking Spikes (throwaway)

> Companion to `2026-08-15-voice-chat-backend-design.md`. Two **throwaway** spikes to run BEFORE
> `writing-plans`. Goal is to **learn**, not to build — every artifact here is disposable and must be
> deleted at the end. Do **not** wire any of this into the real codebase, real tenants, or real secrets.

**Why first:** the backend design (§11) rests on two under-documented ElevenLabs behaviors. Each spike
answers one, and each answer changes concrete spec text. Total budget: ~1 day for both. They share one
setup.

---

## Shared throwaway setup (~30 min)
1. A **free-tier ElevenLabs account** + one **throwaway Agent** (delete after). Voice minutes are consumed —
   keep sessions short.
2. A **tiny local echo server** (throwaway FastAPI, ~40 lines) that:
   - exposes `POST /llm/chat/completions` returning a **canned SSE stream** (OpenAI Chat Completions shape:
     `data: {chunk}\n\n … data: [DONE]\n\n`), and
   - **logs every request** in full — method, path, query, **all headers**, and raw body — to stdout.
   - exposes `POST /tools/{name}` that logs the same and can `sleep(N)` before returning a canned JSON result.
3. A public tunnel to it (`cloudflared tunnel` or `ngrok`) so ElevenLabs can reach it.
4. Point the throwaway Agent's **custom LLM URL** at `<tunnel>/llm/chat/completions`.
5. A browser voice test — ElevenLabs' own agent test page / playground is enough (no need to touch
   `luran-widget` yet).

**Do NOT:** use a real tenant, real ElevenLabs prod key, the real pipeline, or the real action engine. All
throwaway.

---

## Spike 1 — ElevenLabs → our-server auth & metadata passthrough (~half day)

**Unknown:** Exactly how does ElevenLabs authenticate its requests to a **custom** LLM endpoint and to
**server/webhook tools**, and how do we pass + trust `tenant_id` / `luran_session_id`? (Research found this
thinly documented — "bring-your-own-key" is clear, "fully custom server" auth is not.)

**Steps:**
1. With the echo server live, start a voice conversation on the throwaway Agent. **Capture** exactly what
   ElevenLabs sends to `/llm/chat/completions`: which auth header (a static `Authorization` bearer? a custom
   header? the dashboard-set secret/key?), and the body shape (`messages`, `model`, `tools`,
   `elevenlabs_extra_body`, dynamic variables).
2. In the EL dashboard, set a **secret / API key** on the custom-LLM config (if that option exists). Re-run
   and confirm whether/where it appears in the request, so we can verify it server-side.
3. Declare a dummy **server/webhook tool** (`ping`) pointing at `<tunnel>/tools/ping`. Trigger it mid-call.
   **Capture** how EL authenticates the tool webhook — HMAC signature header? shared secret? timestamp? —
   and the payload shape.
4. Set **conversation metadata / dynamic variables** (e.g. `tenant_id`, `luran_session_id`) when starting the
   conversation and confirm **where they surface** in (a) the custom-LLM request and (b) the tool webhook.
5. Send one **forged** request (wrong/missing auth) to confirm we can reject it.

**Success = we can state definitively:**
- The exact mechanism EL uses to authenticate to our custom-LLM endpoint, and to tool webhooks.
- Whether that mechanism is **verifiable** (shared secret / HMAC we can validate) vs. a static key we merely set.
- Exactly how to carry and where to read `tenant_id` + `luran_session_id`.

**Outcomes → spec changes (backend §3, §4, §9):**
- Verifiable signature/secret → validate that header on both endpoints; treat metadata as trusted after the check.
- Only a static key → use a strong per-agent secret, and bind tenant/session via a **server-minted lookup
  token** (issued at token-mint, resolved server-side) rather than trusting client-set metadata alone.
- Limited metadata passthrough → decide the carrier (dynamic variables vs. system-prompt injection vs. the
  lookup token above) and write it into the token-mint design.

**Cleanup:** delete the dummy tool + secret; keep the echo server for Spike 2.

---

## Spike 2 — write-action dead-air UX (~half day)

**Unknown:** Do EL **tool-call sounds** + async tools actually make a realistic multi-second **write**
(booking/quote, ~2–6s) feel natural, or do we need extra verbal mitigation? And do **buffer words** bridge
first-token latency?

**Steps:**
1. Declare a dummy `book_meeting` webhook tool → `<tunnel>/tools/book_meeting`, which **`sleep`s a
   configurable N seconds** then returns a canned success. Enable the tool-call sound on it.
2. In a live voice call, say "book me a meeting Tuesday at 3." Run the tool at **N = 2s, then 4s, then 6s**.
   For each, record the audio and judge: is there audible dead air? Does the tool-call sound play and hold
   the turn? Does the spoken confirmation land cleanly after? Does **barge-in** still work during the wait?
3. Toggle `expects_response` true/false and note the difference (does the agent wait for the real result vs.
   fire-and-forget?).
4. **Buffer words:** make the echo `/llm/chat/completions` delay its first token ~1–2s, once **with** a
   leading `"... "` (ellipsis + trailing space) and once **without**. Confirm the buffer word removes the
   opening dead air.

**Success = a clear verdict on:**
- The **maximum tolerable write latency** with tool-call sounds alone (the latency budget).
- Whether we need a **spoken pre-ack** before the tool call ("give me one moment while I book that…") and/or
  an **async "I'll confirm by email/shortly"** pattern for the slowest writes.
- That buffer words reliably cover in-pipeline first-token latency.

**Outcomes → spec changes (backend §3 buffer words, §4 async/expects_response + a new "pre-tool verbal ack"
rule in the pipeline):**
- Sounds cover up to ~Xs → set the budget; writes over X get a spoken pre-ack + async deferred confirmation.
- Dead air bad even at 2s → require the pipeline to **speak an explicit ack before emitting the tool call**,
  and make the slowest writes async with deferred confirmation.

**Cleanup:** delete the dummy tool + endpoints, the throwaway Agent, the tunnel, and the echo server. Nothing
from these spikes ships.

---

## Deliverable
A short findings note (a few paragraphs) appended to `project_voice_chat` memory + folded into the backend
spec's §3/§4/§9 before `writing-plans` begins. Then discard all spike code.
