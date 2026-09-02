# Handoff — Luran Voice Chat (continue)

**You're resuming the in-widget Voice Chat feature.** A senior session remains the **overall
reviewer** — implement on feature branches and **request review before merging any PR to a deploy
branch**; do not self-merge.

**Read first:** `luran/docs/superpowers/specs/2026-08-15-voice-chat-backend-design.md`, memory
`project_voice_chat`, and `luran/docs/superpowers/handoffs/2026-08-21-voice-continuation-handoff.md`.
Ground any new scope in `docs/luran_prd.md` and cite feature numbers.

## Where it stands (all merged)
- **BE** (`luran`, `develop`): voice backend Slices 1–3 (#77) + `can_use_voice`/
  `max_voice_minutes_per_month` made admin-settable (#80). No open BE PRs.
- **Dashboard** (`luran-dashboard`, `staging`): platform-admin "Voice Chat" enablement toggle +
  minute cap (#58) — a super-admin enables voice per tenant via **Platform Admin → Companies →
  Edit Capabilities**.
- Voice is **dormant by default** (`can_use_voice=false`, secrets `None`→503). To test on staging,
  enable a tenant via that toggle.

## Architecture — SETTLED: keep ElevenLabs
ElevenLabs Agents for the voice runtime with **Luran's pipeline as the custom LLM** → full parity
with text chat (same RAG/KB/flows/tools/lead-scoring/actions). **Do not switch to OpenAI Realtime** —
it's its own brain and would reintroduce the divergence we rejected. (Spike 1 — EL custom-endpoint
auth — is solved: Bearer via participant_name/JWT, `e2acc6d`.)

## Design context (enablement)
Admin-only enablement is the **v1 pilot** (cost control), not permanent. **GA target:** a plan
**tier** grants voice, and the **tenant self-serves** on/off + picks their voice, within their
allotment — never self-granting a paid, per-minute capability or bypassing the mint-time gate
(flag → minute cap → concurrency).

## Your queue (in order; senior reviews each)
1. **Tenant-facing voice dashboard** (`luran-dashboard`, tenant `dashboard/settings`, off `staging`):
   voice on/off toggle for the tenant's own widget, **preferred-voice/persona** selection
   (ElevenLabs voice id per tenant), and a **usage display** (minutes used vs
   `max_voice_minutes_per_month`). This needs a small BE **usage-read endpoint**. (Distinct from the
   existing `brand_voice` tone config.)
2. **Brainstorm the voice subscription model** (use `superpowers:brainstorming`, PRD-grounded): bake
   into tiers (professional/growth+ with a minute allotment) vs a **separate voice add-on / usage
   billing**. Cost-model it (EL ≈ $0.08–0.10/min + our own LLM inference) so allotments stay
   margin-safe. Produce a spec, then implement.
3. **Plan-tier mapping** (`luran` BE): add `can_use_voice`/`max_voice_minutes_per_month` to the
   `subscription_plans` table + seed per tier + include in `plan_service.get_plan_config()`, so a
   tier auto-grants voice via the existing `_update_capabilities_for_tier` sync.
4. **Finish end-to-end**: complete Spikes 2–4 and secure the ElevenLabs API permissions, then
   validate a real call on staging.

## Explicitly NOT now
- Leave the open FE PRs alone for now: `luran-chat #5` (customer voice widget) and `luran_agent #1`
  (agent-caller Flutter) — the reviewer handles those separately.
- Firebase FCM / APNs are **agent-caller** concerns, not voice — don't get pulled into them.

## Process
BE → feature branch off `develop`; dashboard FE → off `staging`; widget (`luran-chat`) → off `main`.
Run SDD per slice. No Claude co-author trailer. Maintain `docs/sop/` + the master checklist on ship.
