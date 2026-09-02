# Handoff — Voice + Agent-Caller continuation

**Date:** 2026-08-21
**From:** senior reviewer session (design + integration + reviews)
**To:** the implementation agent that built the voice/agent-caller tracks
**Reviewer going forward:** the senior session remains the **overall reviewer** — every PR below
is reviewed before merge; the implementation agent does not self-merge to a deploy branch.

Grounding docs: `docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md`,
`.../2026-08-15-agent-caller-backend-escalation-push-design.md`, memories `project_voice_chat`,
`project_agent_caller_app`. Always ground new scope in `docs/luran_prd.md` + cite feature numbers.

---

## 1. Branch/PR state (verified 2026-08-21) — start here

| Repo | Deploy branch | State |
|---|---|---|
| `luran` (BE) | `develop` | ✅ #77 voice BE, #78 agent-caller BE, #79 CD fix, #80 voice-capability admin-settable — all merged. No open PRs. |
| `luran-dashboard` | `staging` | ✅ #58 platform-admin voice toggle merged to `staging`. |
| `luran-widget` | `main` | ✅ #1 env-selectable + #2 staging auto-detect merged. |
| **`luran_agent` (Flutter)** | `main` | ⚠️ **PR #1 OPEN** — agent-caller companion app. Needs review → merge. |
| **`luran-chat` (voice widget FE)** | `main` | ⚠️ **PR #5 OPEN** — customer-facing in-widget voice UI (Slices 1-3). Needs review → merge. |

**FE repos deploy from `main`/`staging`, NOT `develop`** — do not try to merge FE work into develop.

**First actions:** get PRs `luran_agent #1` and `luran-chat #5` review-ready (rebase on their `main`,
confirm CI, address findings), then request review from the senior session before merge.

---

## 2. Architecture decision (settled): continue with **ElevenLabs**

Keep ElevenLabs Agents for the voice runtime with **Luran's pipeline as the custom LLM** (Option C).
Rationale: it's the only path that preserves **full parity** with text chat (same RAG/KB/flows/tools/
lead-scoring/actions). OpenAI's Realtime API is its own brain → would reintroduce the divergence we
rejected. ElevenLabs' voice library also serves the tenant "preferred voice" feature. Do **not** swap
to OpenAI Realtime unless the parity requirement is formally dropped (would be a fresh brainstorm).

Open spikes from the voice track (verify current state before relying on them):
- Spike 1 (EL custom-endpoint auth) — reported solved (Bearer via participant_name/JWT, `e2acc6d`).
- Spikes 2-4 — reported incomplete/stalled. Blocked on: ElevenLabs API perms, Firebase FCM creds,
  APNs key. Confirm and finish these to get a real end-to-end voice call working.

---

## 3. Design context for the pending work

**Enablement model.** v1 is admin-controlled per tenant (spec: per-tenant flag + minute cap, to
control per-minute cost during pilot). GA target (user-directed 2026-08-21): a plan **tier** grants
voice, and the **tenant self-serves** on/off + config within their allotment. Admin-only is the
pilot, not the end state.

**What a tenant should NOT be able to do:** silently incur unlimited per-minute cost. Gating (flag →
minute cap → concurrency, at token mint) stays. Self-serve "on/off" toggles *their own* voice within
what their plan/admin already granted; it does not bypass the cap or self-grant a paid capability.

---

## 4. Pending implementation (the agent's queue; senior reviews each)

**A. Tenant-facing voice dashboard** (`luran-dashboard`, tenant `dashboard/settings`)
- On/off toggle for voice in the tenant's own widget (within an enabled allotment).
- **Preferred voice / persona** selection (ElevenLabs voice id per tenant) — not currently configurable.
- Usage display: minutes used vs `max_voice_minutes_per_month` (needs a BE read endpoint for usage).
- Note: distinct from `brand_voice` (existing AI tone config) — this is voice-CHAT.

**B. Subscription model for voice** (BRAINSTORM FIRST — use superpowers:brainstorming; ground in PRD)
- Decide: bake into existing tiers (e.g. professional/growth+ get voice + a minute allotment) vs a
  **separate voice add-on / usage billing**. Model the cost (EL ~$0.08–0.10/min + our own LLM
  inference) so the allotment/pricing is margin-safe.
- Output a spec, then implement.

**C. Plan-tier mapping (fast-follow the spec already named)** (`luran` BE)
- Add `can_use_voice` / `max_voice_minutes_per_month` to the `subscription_plans` table + seed per
  tier + include in `plan_service.get_plan_config()`, so a tier auto-grants voice via the existing
  `_update_capabilities_for_tier` sync. (Today they're set only via the super-admin capabilities
  endpoint / UI — PR #80 + dashboard #58.)

**D. Finish the FE PRs** (`luran_agent #1`, `luran-chat #5`) — review-ready, then merge.

**E. End-to-end voice** — once EL perms + the spikes land, validate a real call on staging with a
tenant that has `can_use_voice=true` (enable via platform-admin → Companies → Edit Capabilities).

---

## 5. Working agreement
- Implementation agent runs SDD (superpowers:subagent-driven-development) per slice.
- BE → feature branch off `develop`; FE → feature branch off each repo's `main` (dashboard: `staging`).
- No Claude co-author trailer. Maintain SOPs (`docs/sop/`) + the master checklist on ship.
- Request review from the senior session before merging any PR to a deploy branch.
