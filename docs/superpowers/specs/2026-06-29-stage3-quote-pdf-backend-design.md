# Stage 3 — AI Sales Agent · Slice 5b "Quote PDF" — Backend Design

**Date:** 2026-06-29
**Repo:** `luran` (FastAPI · SYNC SQLAlchemy · ARQ · SMTP `EmailService` · Jinja templates)
**Builds on:** Slice 4 (`Quote` model), Slice 5a (`send_rep_alert` ARQ job, `NotificationLog`, `EmailService.send_templated_email`). **Branch `feat/stage3-quote-pdf-backend` stacks on `feat/stage3-notif-email-backend` (Slice 5a PR #35).**
**Decomposition:** Slice 5 = 5a Email ✅ → **5b Quote-PDF (this)** → 5c In-app → 5d Slack → 5e CRM.
**PRD:** `docs/luran_prd.md` §3.5 (quotation delivered as a PDF).
**Status:** Approved (brainstorm) → ready for implementation plan

---

## 1. Context & scope

Generate a PDF of a quote and **attach it to the rep-alert email** that Slice 5a already sends on `quote_generated`. The rep receives a forwardable PDF quote. Backend-only.

**Decisions (brainstorm):**
- **Delivery:** attach the PDF to the rep alert (no visitor email, no consent flow, no widget). The visitor-facing "email me the PDF" consent flow is a later enhancement.
- **Library:** `xhtml2pdf` (pure-pip, no system libraries → no Dockerfile change); render a Jinja HTML template → PDF via `pisa`.

**In scope:** add `xhtml2pdf`; a `render_quote_pdf` service; a `quote_pdf.html` template; an optional-attachment extension to `EmailService`; wire the attachment into `send_rep_alert` for the `quote_generated` event only.

**Out of scope (later):** visitor consent flow + visitor PDF email; per-tenant PDF branding/logos; the widget "email me the PDF" wiring; PDF for lead/meeting alerts.

**Constraint:** PDF generation must **degrade gracefully** — if rendering fails, the rep-alert **text** email still sends (the rep is still notified). Lead/meeting rep alerts are unchanged (no attachment).

---

## 2. Dependency

Add `xhtml2pdf` to `pyproject.toml` (pure-Python; pulls `reportlab` — also pure-pip; no apt-get). `poetry lock` + install.

---

## 3. PDF service

`app/services/quote_pdf_service.py` → `render_quote_pdf(quote, *, lead=None) -> bytes`:
- Builds a context from the `Quote` (`currency`, `line_items` = `[{label, detail, amount}]`, `total`) + optional lead/company.
- Renders Jinja template `quote_pdf.html` to an HTML string, then `xhtml2pdf.pisa.CreatePDF(html, dest=buffer)` → PDF bytes from the buffer.
- Raises (or returns a sentinel the caller treats as failure) if `pisa` reports `err`. Money formatted exactly (`₦{amount:,}`; negative as `−₦…`) consistent with the quote's `currency`.
- Pure in the sense that it takes a quote + context and returns bytes (no DB writes) — unit-testable: assert the result starts with the `%PDF` magic bytes and is non-empty.

---

## 4. `EmailService` attachment support

Extend the shared email layer (`app/services/email_service.py`) **backward-compatibly**:
- `_send_email(..., attachments: list[tuple[str, bytes, str]] | None = None)` — for each `(filename, content, mime)`, attach a `MIMEApplication`/`MIMEBase` part to the existing multipart message (after the HTML/text parts).
- Add `send_templated_email_with_attachments(to_email, subject, template_name, context, attachments)` (or thread `attachments` through `send_templated_email`) so callers can send a templated body + attachments.
- All existing callers are unaffected (default `None`).

---

## 5. Wire into `send_rep_alert`

In the 5a ARQ job `send_rep_alert` (`app/workers/sales_notifications.py`), **only when `log.event == NotifEvent.QUOTE_GENERATED`**:
- Load the `Quote` (already done for the context), `render_quote_pdf(quote, lead=…)`, and send the rep alert **with** the attachment `quote-{quote.id}.pdf`.
- Wrap the render in try/except: on failure, log a warning and send the **text** rep alert without the attachment (still mark the log per the existing `sent`/`failed` rules based on the email send, not the PDF). The PDF is an enhancement to the email, never a reason the rep goes un-notified.
- Lead/meeting events: unchanged (no attachment), and `send_visitor_meeting_confirmation` is untouched.

---

## 6. Template

`app/templates/emails/quote_pdf.html` — invoice-style, **table-based** (xhtml2pdf supports a practical CSS subset: tables + basic styling, NOT flexbox/grid). Sections: a header (tenant/company name, "Quotation"), a line-items table (label · detail · amount), and a total row. Reuse the email visual tone where the CSS subset allows.

---

## 7. Safety & invariants

- Backend-only; no widget; no visitor email.
- Graceful degradation: PDF-render failure never blocks the rep-alert email or breaks the worker (the worker's existing retry/idempotency rules still apply to the email send).
- Attachment only on `quote_generated`; no behavioral change to lead/meeting alerts or the visitor confirmation.
- Tenant-scoped (the quote + lead are already tenant-scoped via the log).
- No new PII; the PDF contains the same quote data already stored.

---

## 8. Testing (pytest)

- **`render_quote_pdf`**: returns non-empty bytes starting with `%PDF` for a normal quote (base + overage + discount line items); handles a single-line quote and a quote with a negative (discount) amount without error.
- **`EmailService` attachment**: a message built with one attachment has the expected MIME part (filename + application part) — assert at the message-construction boundary with SMTP mocked.
- **`send_rep_alert` quote path**: for `quote_generated`, `send_templated_email...` is called WITH an attachment whose name is `quote-{id}.pdf`; for `sales_ready_lead`/`meeting_booked`, called WITHOUT an attachment; a `render_quote_pdf` exception → the text email is still sent (no attachment) and the log still resolves `sent`. Mock `EmailService` + `render_quote_pdf`.
- Full suite green; Slice 5a behavior unchanged except the quote rep-alert now carries a PDF.

---

## 9. Files

**New:** `app/services/quote_pdf_service.py`, `app/templates/emails/quote_pdf.html`, `tests/sales/test_quote_pdf.py`, `tests/workers/test_rep_alert_pdf.py` (or extend `tests/workers/test_sales_notifications.py`).
**Modify:** `pyproject.toml` (+`xhtml2pdf`), `app/services/email_service.py` (attachment support), `app/workers/sales_notifications.py` (attach PDF on quote event).

---

## 10. Acceptance (PRD §3.5)

1. A generated quote produces a PDF attached to the rep-alert email.
2. The PDF renders the quote's line items + total (exact currency formatting).
3. PDF-render failure degrades gracefully — the rep still gets the text alert.
4. Lead/meeting alerts and the visitor confirmation are unchanged. No Dockerfile/system-dep changes. All new + existing tests green.
