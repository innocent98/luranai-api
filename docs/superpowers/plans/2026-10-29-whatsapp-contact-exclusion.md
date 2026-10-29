# WhatsApp contact exclusion (T15) — implementation plan

Spec: `docs/superpowers/specs/2026-10-29-whatsapp-contact-exclusion-design.md`. Test-first per task.

- [ ] T1 Model `WhatsAppContactExclusion` (`app/db/models/whatsapp.py`, export in `models/__init__.py`) + Alembic `wa40contactexclusion` (down `cl10conversationlimits`); migration round trip on scratch Postgres.
- [ ] T2 `app/services/whatsapp_exclusion.py`: `normalize_exclusion_phone`, `find_exclusion` (phone / JID-derived / learned LID), `learn_lid`, `purge_sessionless_chat`, CRUD service (add, bulk, list, remove) with gating, own-number, cap.
- [ ] T3 Schemas + endpoints under `/api/v1/whatsapp/connections/{id}/exclusions`.
- [ ] T4 Inbound: check in `handle_inbound` and `handle_outbound_external` before `_record_message_id`; early drop in the webhook before `enqueue_event`.
- [ ] T5 Sender: `_preflight` drops any send to an excluded chat.
- [ ] T6 Account purge explicit delete; tests (API, inbound kinds, owner-typed, sender, learning, Postgres path).
- [ ] T7 Live capture against the running app -> `docs/fe-integration-guide-whatsapp-exclusion.md`; SOP; checklist T15.
- [ ] T8 ruff/black/tests/migration; Opus review; PR to develop (not merged).
