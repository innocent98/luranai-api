# Stage 3 Slice 5b — Quote PDF (Backend / luran) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render a quote as a PDF and attach it to the rep-alert email that Slice 5a already sends on `quote_generated`.

**Architecture:** A pure `render_quote_pdf(quote, lead)` renders a Jinja template via `xhtml2pdf` → PDF bytes; `EmailService` gains backward-compatible attachment support; the 5a `send_rep_alert` job attaches the PDF only for the quote event, degrading gracefully (text email still sends if the PDF fails).

**Tech Stack:** FastAPI, SYNC SQLAlchemy, ARQ, SMTP `EmailService` + Jinja, `xhtml2pdf` (pure-pip).

## Global Constraints

- **Backend-only.** No widget, no visitor email, no Dockerfile/system-dep change (`xhtml2pdf` is pure-pip; pulls `reportlab`, also pure-pip).
- **Graceful degradation:** a PDF-render failure NEVER blocks the rep-alert email or breaks the worker — the rep still gets the text alert. PDF is an enhancement to a working email, not a new failure mode.
- Attachment ONLY on `event == NotifEvent.QUOTE_GENERATED`. Lead/meeting alerts + `send_visitor_meeting_confirmation` unchanged.
- Existing `EmailService` callers must be unaffected (attachment param defaults to `None`; the no-attachment path stays byte-identical — keeps `MIMEMultipart("alternative")`).
- Templates live in `app/templates/emails/` (loaded by `EmailService.render_template("<name>.html", **ctx)`).
- Money formatted exactly, currency-aware: reuse `app.sales.pricing._currency_symbol(currency)` (e.g. `₦{amount:,}`, negative `−₦…`).
- SYNC SQLAlchemy. No Claude co-author trailer. Branch `feat/stage3-quote-pdf-backend` stacks on `feat/stage3-notif-email-backend` (5a) — `Quote`, `send_rep_alert`, `NotificationLog` already exist.
- Tests: `poetry run pytest <path> -v`, `DATABASE_URL=postgresql://luran_user:luran_password_dev@localhost:5433/luran_db` (start `docker start luran_test_db` if 5433 refuses). Use `db_session`.

---

### Task 1: `xhtml2pdf` dependency + `render_quote_pdf` service + template

**Files:**
- Modify: `pyproject.toml`
- Create: `app/services/quote_pdf_service.py`, `app/templates/emails/quote_pdf.html`
- Test: `tests/sales/test_quote_pdf.py`

**Interfaces:**
- Produces: `render_quote_pdf(quote, *, lead=None) -> bytes` — renders `quote_pdf.html` to PDF; raises `RuntimeError` if `pisa` reports an error.

- [ ] **Step 1: Add the dependency**

```bash
cd /Users/adebayovictor/Documents/dev/luran/luran
poetry add xhtml2pdf
```
Confirm it resolves (pure-pip; no apt-get). `poetry.lock` updates.

- [ ] **Step 2: Write the failing test**

```python
# tests/sales/test_quote_pdf.py
from uuid import uuid4

from app.db.models.quote import Quote, QuoteStatus
from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.services.quote_pdf_service import render_quote_pdf


def _quote(db):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="Acme", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Northwind")
    db.add(lead); db.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN", total=62050,
              line_items=[{"label": "Growth", "detail": "11–50 users", "amount": 45000},
                          {"label": "Extra seats (×8)", "detail": "₦3,500 / seat", "amount": 28000},
                          {"label": "Annual discount", "detail": "−15%", "amount": -10950}],
              status=QuoteStatus.DRAFT)
    db.add(q); db.flush(); db.refresh(q)
    return q, lead


def test_render_quote_pdf_returns_pdf_bytes(db_session):
    q, lead = _quote(db_session)
    data = render_quote_pdf(q, lead=lead)
    assert isinstance(data, bytes) and len(data) > 0
    assert data[:5] == b"%PDF-"  # PDF magic header


def test_render_single_line_quote(db_session):
    q, lead = _quote(db_session)
    q.line_items = [{"label": "Starter", "detail": "1–10 users", "amount": 20000}]
    q.total = 20000
    data = render_quote_pdf(q, lead=lead)
    assert data[:5] == b"%PDF-"
```

- [ ] **Step 3: Run to verify it fails**

Run: `poetry run pytest tests/sales/test_quote_pdf.py -v`
Expected: FAIL (module missing).

- [ ] **Step 4: Implement the service**

```python
# app/services/quote_pdf_service.py
"""Render a Quote to a PDF (Stage 3 Slice 5b). Pure-pip xhtml2pdf; no system deps."""

from io import BytesIO

from xhtml2pdf import pisa

from app.sales.pricing import _currency_symbol
from app.services.email_service import EmailService


def _fmt(amount: int, currency: str) -> str:
    sym = _currency_symbol(currency)
    sign = "−" if amount < 0 else ""
    return f"{sign}{sym}{abs(int(amount)):,}"


def render_quote_pdf(quote, *, lead=None) -> bytes:
    """Render `quote` to PDF bytes. Raises RuntimeError if generation fails."""
    items = [
        {"label": li.get("label", ""), "detail": li.get("detail", ""),
         "amount_fmt": _fmt(int(li.get("amount", 0)), quote.currency),
         "negative": int(li.get("amount", 0)) < 0}
        for li in (quote.line_items or [])
    ]
    ctx = {
        "company": (lead.company if lead is not None and lead.company else "Your company"),
        "currency": quote.currency,
        "items": items,
        "total_fmt": _fmt(int(quote.total or 0), quote.currency),
    }
    html = EmailService().render_template("quote_pdf.html", **ctx)
    buf = BytesIO()
    result = pisa.CreatePDF(src=html, dest=buf)
    if result.err:
        raise RuntimeError("quote PDF generation failed")
    return buf.getvalue()
```

- [ ] **Step 5: Create the template**

`app/templates/emails/quote_pdf.html` — table-based (xhtml2pdf supports a CSS subset: tables + basic styling, NO flexbox/grid):

```html
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body { font-family: Helvetica, Arial, sans-serif; color: #1a1a2e; font-size: 12px; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .muted { color: #6b7280; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #e5e7eb; }
  td.amt, th.amt { text-align: right; }
  .neg { color: #16a34a; }
  tr.total td { border-top: 2px solid #1a1a2e; border-bottom: none; font-weight: bold; font-size: 14px; padding-top: 10px; }
</style></head><body>
  <h1>Quotation</h1>
  <div class="muted">Prepared for {{ company }}</div>
  <table>
    <tr><th>Item</th><th>Detail</th><th class="amt">Amount</th></tr>
    {% for it in items %}
    <tr><td>{{ it.label }}</td><td class="muted">{{ it.detail }}</td>
        <td class="amt {% if it.negative %}neg{% endif %}">{{ it.amount_fmt }}</td></tr>
    {% endfor %}
    <tr class="total"><td colspan="2">Estimated total</td><td class="amt">{{ total_fmt }}</td></tr>
  </table>
</body></html>
```

- [ ] **Step 6: Run tests**

Run: `poetry run pytest tests/sales/test_quote_pdf.py -v`
Expected: PASS (2). (If `EmailService().render_template` can't find the template, confirm the loader dir is `app/templates/emails/` per existing email templates.)

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml poetry.lock app/services/quote_pdf_service.py app/templates/emails/quote_pdf.html tests/sales/test_quote_pdf.py
git commit -m "feat(notifications): quote PDF rendering via xhtml2pdf"
```

---

### Task 2: `EmailService` attachment support

**Files:**
- Modify: `app/services/email_service.py`
- Test: `tests/services/test_email_attachments.py`

**Interfaces:**
- Produces: `_send_email(..., attachments: list[tuple[str, bytes, str]] | None = None) -> bool` and `send_templated_email_with_attachments(to_email, subject, template_name, context, attachments) -> bool`. `attachments` = `[(filename, content_bytes, mime_type)]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_email_attachments.py
from unittest.mock import patch
from app.services.email_service import EmailService


def test_send_email_with_attachment_builds_mixed_message():
    captured = {}

    def fake_send_message(msg):
        captured["msg"] = msg

    class FakeServer:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, *a, **k): pass
        def login(self, *a): pass
        def send_message(self, msg): fake_send_message(msg)

    with patch("app.services.email_service.smtplib.SMTP", return_value=FakeServer()):
        ok = EmailService()._send_email(
            to_email="rep@co.com", subject="Quote", html_content="<p>hi</p>",
            attachments=[("quote-1.pdf", b"%PDF-1.4 fake", "application/pdf")])
    assert ok is True
    msg = captured["msg"]
    assert msg.get_content_subtype() == "mixed"
    filenames = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert "quote-1.pdf" in filenames


def test_send_email_without_attachment_is_alternative():
    class FakeServer:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, *a, **k): pass
        def login(self, *a): pass
        def send_message(self, msg): pass

    with patch("app.services.email_service.smtplib.SMTP", return_value=FakeServer()):
        ok = EmailService()._send_email(to_email="r@co.com", subject="s", html_content="<p>x</p>")
    assert ok is True
```

(Adapt the SMTP fake to the SSL/STARTTLS branch the test settings use — check whether `settings.SMTP_PORT == 465` in the test env and patch `smtplib.SMTP_SSL` instead if so.)

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/services/test_email_attachments.py -v`
Expected: FAIL (`attachments` not accepted).

- [ ] **Step 3: Implement**

In `app/services/email_service.py`: add imports `from email.mime.application import MIMEApplication`. Change `_send_email` signature to add `attachments: list[tuple[str, bytes, str]] | None = None`. Replace the message-construction block (lines ~52–60) with:

```python
            if attachments:
                msg = MIMEMultipart("mixed")
                msg["From"] = f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>"
                msg["To"] = to_email
                msg["Subject"] = subject
                body = MIMEMultipart("alternative")
                if text_content:
                    body.attach(MIMEText(text_content, "plain"))
                body.attach(MIMEText(html_content, "html"))
                msg.attach(body)
                for filename, content, mime in attachments:
                    subtype = mime.split("/", 1)[1] if "/" in mime else "octet-stream"
                    part = MIMEApplication(content, _subtype=subtype)
                    part.add_header("Content-Disposition", "attachment", filename=filename)
                    msg.attach(part)
            else:
                msg = MIMEMultipart("alternative")
                msg["From"] = f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>"
                msg["To"] = to_email
                msg["Subject"] = subject
                if text_content:
                    msg.attach(MIMEText(text_content, "plain"))
                msg.attach(MIMEText(html_content, "html"))
```

Then add the convenience method:

```python
    def send_templated_email_with_attachments(
        self, to_email: str, subject: str, template_name: str,
        context: dict, attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> bool:
        html_content = self.render_template(template_name, **context)
        return self._send_email(to_email=to_email, subject=subject,
                                html_content=html_content, attachments=attachments)
```

- [ ] **Step 4: Run tests**

Run: `poetry run pytest tests/services/test_email_attachments.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/email_service.py tests/services/test_email_attachments.py
git commit -m "feat(email): optional binary attachment support (backward-compatible)"
```

---

### Task 3: Attach the PDF in `send_rep_alert` (quote event only)

**Files:**
- Modify: `app/workers/sales_notifications.py`
- Test: extend `tests/workers/test_sales_notifications.py` (or new `tests/workers/test_rep_alert_pdf.py`)

**Interfaces:**
- Consumes: `render_quote_pdf` (Task 1), `send_templated_email_with_attachments` (Task 2).

- [ ] **Step 1: Write the failing test**

```python
# tests/workers/test_rep_alert_pdf.py  (or extend test_sales_notifications.py)
from uuid import uuid4
from unittest.mock import patch

from app.db.models.tenant import Tenant
from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus
from app.db.models.notification_log import NotificationLog, NotifEvent, NotifChannel, NotifStatus
from app.workers.sales_notifications import send_rep_alert


def _seed_quote_log(db):
    t = Tenant(name=f"t_{uuid4().hex[:8]}", display_name="T", api_key_hash=uuid4().hex,
               subscription_tier="trial", subscription_status="trialing")
    db.add(t); db.flush()
    lead = Lead(tenant_id=t.id, conversation_id=uuid4(), email="v@co.com", company="Acme")
    db.add(lead); db.flush()
    q = Quote(tenant_id=t.id, lead_id=lead.id, currency="NGN",
              line_items=[{"label": "Growth", "detail": "", "amount": 45000}], total=45000,
              status=QuoteStatus.DRAFT)
    db.add(q); db.flush()
    log = NotificationLog(tenant_id=t.id, event=NotifEvent.QUOTE_GENERATED, channel=NotifChannel.EMAIL,
                          entity_type="quote", entity_id=q.id, recipient="rep@co.com", status=NotifStatus.PENDING)
    db.add(log); db.commit()
    return log.id


async def test_quote_rep_alert_has_pdf_attachment(db_session, monkeypatch):
    log_id = _seed_quote_log(db_session)
    monkeypatch.setattr("app.workers.sales_notifications.SessionLocal", lambda: db_session)
    with patch("app.services.email_service.EmailService.send_templated_email_with_attachments", return_value=True) as send:
        await send_rep_alert({}, str(log_id))
    send.assert_called_once()
    attachments = send.call_args.kwargs.get("attachments") or (send.call_args.args[4] if len(send.call_args.args) > 4 else None)
    assert attachments and attachments[0][0] == f"quote-{...}"  # filename startswith "quote-"; adapt assertion


async def test_quote_rep_alert_sends_text_when_pdf_fails(db_session, monkeypatch):
    log_id = _seed_quote_log(db_session)
    monkeypatch.setattr("app.workers.sales_notifications.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.workers.sales_notifications.render_quote_pdf",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    # falls back to the plain templated send; log still resolves SENT
    with patch("app.services.email_service.EmailService.send_templated_email", return_value=True) as send_plain:
        await send_rep_alert({}, str(log_id))
    send_plain.assert_called_once()
```

(Adapt the monkeypatch/async style + the attachment-arg assertion to the file's existing conventions — read `tests/workers/test_sales_notifications.py` first.)

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/workers/test_rep_alert_pdf.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `app/workers/sales_notifications.py` `send_rep_alert`, import `from app.services.quote_pdf_service import render_quote_pdf` and `from app.db.models.quote import Quote`. After building `ctx_data` and resolving the recipient/subject, branch:

```python
        if log.event == NotifEvent.QUOTE_GENERATED:
            quote = db.get(Quote, log.entity_id)
            lead = db.get(Lead, quote.lead_id) if quote else None
            pdf_bytes = None
            try:
                if quote is not None:
                    pdf_bytes = render_quote_pdf(quote, lead=lead)
            except Exception as pdf_exc:  # noqa: BLE001 — PDF is an enhancement, never block the alert
                logger.warning(f"quote PDF render failed (sending text alert): {pdf_exc}")
            if pdf_bytes:
                ok = EmailService().send_templated_email_with_attachments(
                    to_email=log.recipient, subject=_REP_SUBJECT.get(log.event, "Sales notification"),
                    template_name="sales_rep_alert.html", context=ctx_data,
                    attachments=[(f"quote-{log.entity_id}.pdf", pdf_bytes, "application/pdf")])
            else:
                ok = EmailService().send_templated_email(
                    to_email=log.recipient, subject=_REP_SUBJECT.get(log.event, "Sales notification"),
                    template_name="sales_rep_alert.html", context=ctx_data)
        else:
            ok = EmailService().send_templated_email(
                to_email=log.recipient, subject=_REP_SUBJECT.get(log.event, "Sales notification"),
                template_name="sales_rep_alert.html", context=ctx_data)
```

Then the existing `log.status = SENT if ok else FAILED` (with the `_EmailSendFailed` raise on False) applies to `ok` exactly as before. Read the current `send_rep_alert` body and splice this in place of the single existing `EmailService().send_templated_email(...)` call — keep the SENT short-circuit, the `_EmailSendFailed` retry semantics, and `finally: db.close()` intact. `send_visitor_meeting_confirmation` is untouched.

- [ ] **Step 4: Run tests + regression**

Run: `poetry run pytest tests/workers/test_rep_alert_pdf.py tests/workers/test_sales_notifications.py -v`
Expected: new tests PASS; the existing 5a worker tests still PASS (lead/meeting alerts have no attachment; quote alert now does).

- [ ] **Step 5: Commit**

```bash
git add app/workers/sales_notifications.py tests/workers/
git commit -m "feat(notifications): attach quote PDF to the rep alert (quote event)"
```

---

### Task 4: Full-suite verification

- [ ] **Step 1:** Ensure the test DB is up (`docker start luran_test_db`), then `poetry run pytest -q -p no:cacheprovider --no-cov` (DATABASE_URL on 5433). Expected: all green; 5a behavior unchanged except the quote rep-alert now carries a PDF.
- [ ] **Step 2:** Commit fixups if any: `git commit -am "test(notifications): quote-pdf suite green"`.

---

## Self-Review notes
- Spec §2 dep → Task 1. §3 service → Task 1. §4 email attachment → Task 2. §5 wiring → Task 3. §6 template → Task 1. §7 invariants (graceful degradation, quote-only, backward-compat) → Tasks 2/3. §8 testing → per task + Task 4. §10 acceptance covered.
- Names consistent: `render_quote_pdf(quote, *, lead=None)`, `send_templated_email_with_attachments`, `_send_email(..., attachments=...)`, `quote_pdf.html`, attachment filename `quote-{id}.pdf`.
- Backward-compat: the no-attachment `_send_email` path is preserved byte-identical; default `attachments=None`.
- The implementer MUST: confirm the email template loader dir (`app/templates/emails/`) so `render_template("quote_pdf.html")` resolves; check whether the test SMTP path is SSL (465) or STARTTLS and patch accordingly (Task 2); splice the Task 3 branch WITHOUT disturbing the `_EmailSendFailed`/SENT/`finally db.close()` semantics; adapt the worker-test monkeypatch + attachment-arg assertion to the existing `test_sales_notifications.py` conventions.
