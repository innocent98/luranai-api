# Spike 1 — findings (RAN LIVE 2026-08-19)

Companion to `2026-08-15-voice-chat-spikes.md`. Spike 1 is **CLOSED**. Every claim below
was captured on the wire from a real ElevenLabs agent (28 requests), not read from docs.

## Q_A — How does ElevenLabs authenticate to our custom-LLM endpoint?

**Static bearer token.**

```
Authorization: Bearer <the secret you set in the agent's custom-LLM config>
User-Agent: AsyncOpenAI/Python 2.45.0
```

Not HMAC, not a timestamped signature. `verify_elevenlabs_request` as shipped is **already
correct** — `hmac.compare_digest` against a bearer value. No swap required; the
`TODO(spike-1)` in `auth.py` can close.

## Q_B — How does our per-session token reach the endpoint?

### What does NOT work (all eliminated by capture, not by reasoning)

| Carrier | Result |
|---|---|
| `elevenlabs_extra_body` | **Absent.** Documented by ElevenLabs, never sent in practice. |
| `dynamic_variables` in the body | **Absent.** |
| `user` in the body | **Absent.** |
| `dynamicVariables` via client `startSession()` | **Not forwarded**, and does not drive prompt substitution either. |

The custom-LLM body contains exactly, and only:
`messages · model · max_tokens · stream · stream_options · temperature · tools`

### What DOES work

```
1. Mint server-side:  GET /v1/convai/conversation/token
                          ?agent_id=<id>&participant_name=<voice_token>
                      header: xi-api-key: <key with convai_write>
                      returns: { token, conversation_id }

2. ElevenLabs binds participant_name into the session JWT as  name  and  sub.

3. The agent's prompt contains the line:   Session: {{voice_token}}

4. ElevenLabs substitutes it. It arrives in messages[0].content:
                                           Session: <voice_token>
```

**Negative control:** with the same prompt but no `participant_name`, the literal
`{{voice_token}}` arrives unsubstituted. Substitution is genuinely driven by the mint
parameter.

**Why this shape is good:** server-side (the browser never chooses the value),
per-session (bound to a ~15-minute JWT for one conversation), and unforgeable (a client
cannot inject another tenant's token without us minting it).

## Additional findings the spike surfaced

- **Escalation was dead end-to-end.** ElevenLabs passes `escalate_to_human` to *our* LLM
  as an OpenAI function in `tools` and only invokes the browser client tool once we emit a
  `tool_calls` delta. `stream_reply` only ever emitted text, so the tool could never fire.
  Confirmed live: the caller asked for a human twice; the client tool stayed silent.
- **`ELEVENLABS_API_KEY` needs the `convai_write` permission.** A read-only key fails the
  mint with `401 missing_permissions`. Recorded in `.env.example`.
- **A blank agent with no knowledge base works.** The system prompt arrives carrying
  `"No additional documentation provided"`. Luran remains the brain, as intended.
- **The first message is ElevenLabs-authored** ("Hello, how can I help you today?"), spoken
  before any LLM turn. That is genuine coupling — it lives in agent config, not in Luran.
- **The buffer word is audible and working.** Replies arrive as `"... <text>"`, covering
  pipeline latency exactly as designed.

## Required agent configuration (not optional)

| Field | Value |
|---|---|
| Template | Blank — not a prefilled persona |
| Knowledge base | Empty |
| Prompt / persona | Must contain `Session: {{voice_token}}` — **load-bearing**, not cosmetic |
| LLM | Custom LLM → `POST /api/v1/voice/llm/chat/completions` |
| Auth | `Bearer <VOICE_SHARED_SECRET>` |
| Tools | One **client** tool named exactly `escalate_to_human` |

## Still open

**Spike 2** (write-action dead-air) has not run. Slice 4 (write-action tool webhooks)
remains deliberately unbuilt pending it.
