#!/usr/bin/env python
"""Drive a LOCALLY RUNNING Luran API through the whole referral flow and save the real traffic.

Every call is made over HTTP (httpx) against ``--base-url`` and written to
``docs/fe-integration-captures/referral/NN-short-name.json`` as::

    {"description", "note"?, "request": {method, path, query, headers, body},
     "response": {status, headers, body}, "redacted": [...]}

Bodies are stored verbatim (parsed JSON; text when not JSON). The only edits are secrets: token and
cookie values and the webhook signature become ``"<redacted>"`` (listed under ``redacted``).
Request headers keep only ``content-type`` and the NAMES of cookies sent.

Steps that cannot go over HTTP are done through the project's own services against the scratch
database and said so in the capture's ``note``: creating the first super admin, reading the
one-time invite / reset token out of Redis (email delivery is never exercised), inserting the
pending ``PaymentTransaction`` (checkout would call the real Paystack), recording the widget host
that feeds the shared-domain flag, and running the promote / prepare-batch job functions.

SAFETY. The script refuses to run unless DATABASE_URL names the scratch database ``ref_capture``
and REDIS_URL uses logical database 5. Run it with the project venv, against an app started with
the same environment, on a scratch Postgres/Redis, never against anything real::

    python scripts/capture_referral.py --base-url http://127.0.0.1:18765

All data is an obvious placeholder (``@example.com`` emails, bank account ``0000000000``, bank
code ``000``). The bank account number must appear only in the REQUEST that creates the marketer.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # so `app` imports work when run as `python scripts/...`
DEFAULT_OUT = REPO / "docs" / "fe-integration-captures" / "referral"

SCRATCH_DB = "ref_capture"
SCRATCH_REDIS_DB = "5"

SUPER_EMAIL = "super.placeholder@example.com"
PLAIN_ADMIN_EMAIL = "admin.placeholder@example.com"
ADMIN_PASSWORD = "Placeholder-Pass-123!"
# A different domain from the signup emails (@example.com): a signup whose email shares the
# marketer's (non-webmail) domain is treated as a self-referral and is never rewarded.
MARKETER_EMAIL = "ada.placeholder@example.org"
MARKETER_CODE = "ada-placeholder"
MARKETER_PASSWORD = "Placeholder-Pass-123!"
MARKETER_NEW_PASSWORD = "Placeholder-Pass-456!"
BANK_CODE = "000"
ACCOUNT_NUMBER = "0000000000"  # must appear only in the marketer-creation request
SHARED_HOST = "shop.placeholder-stores.example.com"

SECRET_KEYS = {"token", "access_token", "refresh_token"}
REDACTED = "<redacted>"


# --------------------------------------------------------------------------------- safety


def _guard_environment() -> None:
    db_name = urlsplit(os.environ.get("DATABASE_URL", "")).path.rsplit("/", 1)[-1]
    redis_db = urlsplit(os.environ.get("REDIS_URL", "")).path.lstrip("/")
    if db_name != SCRATCH_DB or redis_db != SCRATCH_REDIS_DB:
        sys.exit(
            f"refusing to run: DATABASE_URL must name the scratch database '{SCRATCH_DB}' and "
            f"REDIS_URL must use logical database {SCRATCH_REDIS_DB}"
        )


# --------------------------------------------------------------------------------- cookie jar


class Jar:
    """A minimal cookie jar. httpx's own jar would refuse the Secure cookies over plain http."""

    def __init__(self) -> None:
        self._cookies: dict[str, tuple[str, str]] = {}  # name -> (value, path)

    def absorb(self, response: httpx.Response) -> None:
        for raw in response.headers.get_list("set-cookie"):
            parsed = SimpleCookie()
            parsed.load(raw)
            for name, morsel in parsed.items():
                deleted = morsel.value in ("", '""') or morsel["max-age"] == "0"
                if deleted:
                    self._cookies.pop(name, None)
                else:
                    self._cookies[name] = (morsel.value, morsel["path"] or "/")

    def _for(self, path: str) -> dict[str, str]:
        return {
            n: v for n, (v, p) in self._cookies.items() if path.startswith(p.rstrip("/") or "/")
        }

    def header(self, path: str) -> str | None:
        sent = self._for(path)
        return "; ".join(f"{n}={v}" for n, v in sent.items()) or None

    def names(self, path: str) -> list[str]:
        return sorted(self._for(path))

    def value(self, name: str) -> str | None:
        entry = self._cookies.get(name)
        return entry[0] if entry else None

    def clear(self) -> None:
        self._cookies.clear()


# --------------------------------------------------------------------------------- recording


def _redact(value, found: set[str]):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in SECRET_KEYS and isinstance(item, str):
                found.add(f"body.{key}")
                out[key] = REDACTED
            else:
                out[key] = _redact(item, found)
        return out
    if isinstance(value, list):
        return [_redact(item, found) for item in value]
    return value


def _redact_set_cookie(raw: str) -> str:
    return re.sub(r"^([^=;]+)=[^;]*", rf"\1={REDACTED}", raw)


class Recorder:
    def __init__(self, base_url: str, out_dir: Path) -> None:
        self.client = httpx.Client(base_url=base_url, timeout=30.0)
        self.out_dir = out_dir
        self.counter = 0
        self.index: list[tuple[str, str, str, int]] = []  # file, description, "METHOD path", status
        self.mismatches: list[str] = []

    def call(
        self,
        slug: str,
        description: str,
        method: str,
        path: str,
        *,
        body=None,
        raw_content: bytes | None = None,
        query: dict | None = None,
        jar: Jar | None = None,
        headers: dict[str, str] | None = None,
        redact_request_headers: tuple[str, ...] = (),
        note: str | None = None,
        expect: int | None = None,
    ) -> httpx.Response:
        self.counter += 1
        file_name = f"{self.counter:02d}-{slug}.json"
        send_headers = dict(headers or {})
        cookie_header = jar.header(path) if jar else None
        cookie_names_sent = jar.names(path) if jar else []
        if "cookie" in send_headers and not cookie_header:  # a cookie set by hand for this call
            cookie_names_sent = sorted(
                {part.split("=", 1)[0].strip() for part in send_headers["cookie"].split(";")}
            )
        if cookie_header:
            send_headers["cookie"] = cookie_header
        if raw_content is not None:
            send_headers.setdefault("content-type", "application/json")
            response = self.client.request(
                method, path, params=query, content=raw_content, headers=send_headers
            )
            request_body = json.loads(raw_content)
        else:
            response = self.client.request(
                method, path, params=query, json=body, headers=send_headers
            )
            request_body = body
        if jar:
            jar.absorb(response)

        found: set[str] = set()
        request_headers: dict = {}
        if body is not None or raw_content is not None:
            request_headers["content-type"] = "application/json"
        if cookie_names_sent:
            request_headers["cookies_sent"] = cookie_names_sent
        for name in redact_request_headers:
            request_headers[name] = REDACTED
            found.add(f"request.header.{name}")
        if "authorization" in {k.lower() for k in (headers or {})}:
            request_headers["authorization"] = "Bearer <redacted>"
            found.add("request.header.authorization")

        try:
            response_body = response.json()
        except ValueError:
            response_body = response.text
        response_headers = {"content-type": response.headers.get("content-type")}
        set_cookies = response.headers.get_list("set-cookie")
        if set_cookies:
            response_headers["set-cookie"] = [_redact_set_cookie(c) for c in set_cookies]
            found.add("response.set-cookie values")

        record = {
            "description": description,
            "request": {
                "method": method,
                "path": path,
                "query": query or {},
                "headers": request_headers,
                "body": _redact(request_body, found),
            },
            "response": {
                "status": response.status_code,
                "headers": response_headers,
                "body": _redact(response_body, found),
            },
        }
        if note:
            record["note"] = note
        if found:
            record["redacted"] = sorted(found)
        (self.out_dir / file_name).write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n"
        )

        self.index.append((file_name, description, f"{method} {path}", response.status_code))
        flag = ""
        if expect is not None and response.status_code != expect:
            flag = f"  <-- EXPECTED {expect}"
            self.mismatches.append(f"{file_name}: expected {expect}, got {response.status_code}")
        print(f"{file_name:58s} {response.status_code}{flag}")
        return response


# --------------------------------------------------------------------------------- the flow


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    _guard_environment()

    # Imported only after the guard: these read the (scratch) environment at import time.
    import redis
    from sqlalchemy import select, text

    from app.core.config import settings
    from app.core.security import create_access_token
    from app.db.models.payment import PaymentTransaction
    from app.db.models.platform_admin import PlatformAdminRole
    from app.db.models.referral import PayoutBatch
    from app.db.models.user import User
    from app.db.session import SessionLocal
    from app.schemas.platform_admin import PlatformAdminCreate
    from app.services.platform_admin_auth_service import PlatformAdminAuthService
    from app.services.referral.payouts import prepare_batch
    from app.services.referral.rewards import promote_pending

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.json"):
        stale.unlink()
    rec = Recorder(args.base_url, out_dir)
    redis_client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    db = SessionLocal()
    db.get_bind().echo = False

    admin_jar, plain_jar, marketer_jar = Jar(), Jar(), Jar()
    anon = Jar()

    # --- 0. first super admin: the API has no bootstrap endpoint, so use the project's service
    try:
        PlatformAdminAuthService(db).create_admin(
            PlatformAdminCreate(
                email=SUPER_EMAIL,
                password=ADMIN_PASSWORD,
                full_name="Sam Placeholder",
                role=PlatformAdminRole.SUPER_ADMIN,
            )
        )
    except ValueError:
        pass  # already there from an earlier run against the same scratch database
    db.commit()

    # --- 1. platform admins
    rec.call(
        "platform-admin-login",
        "Platform super admin signs in (cookies set; the body also carries the access token).",
        "POST",
        "/api/v1/platform/auth/login",
        body={"email": SUPER_EMAIL, "password": ADMIN_PASSWORD},
        jar=admin_jar,
        note=(
            "The super admin row was created beforehand with PlatformAdminAuthService.create_admin "
            "against the scratch database (there is no bootstrap endpoint)."
        ),
        expect=200,
    )
    rec.call(
        "platform-create-plain-admin",
        "Super admin creates a plain ADMIN (used later to show the 403 on writes).",
        "POST",
        "/api/v1/platform/auth/admins",
        body={
            "email": PLAIN_ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "full_name": "Pat Placeholder",
            "role": "ADMIN",
        },
        jar=admin_jar,
        expect=201,
    )
    rec.call(
        "platform-plain-admin-login",
        "The plain ADMIN signs in.",
        "POST",
        "/api/v1/platform/auth/login",
        body={"email": PLAIN_ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        jar=plain_jar,
        expect=200,
    )

    # --- 2. admin creates the marketer (+ error shapes)
    created = rec.call(
        "admin-create-marketer",
        "Super admin onboards a marketer. Bank details go in; only bank_last4 ever comes back. "
        "Email delivery of the invite is not exercised live (SMTP points at an unused port).",
        "POST",
        "/api/v1/platform/referrals/",
        body={
            "name": "Ada Placeholder",
            "email": MARKETER_EMAIL,
            "code": MARKETER_CODE,
            "bank_code": BANK_CODE,
            "account_number": ACCOUNT_NUMBER,
            "account_name": "Ada Placeholder",
        },
        jar=admin_jar,
        expect=201,
    )
    marketer_id = created.json()["id"]
    rec.call(
        "admin-create-marketer-duplicate-code",
        "Error: the referral code is already taken (different email, same code).",
        "POST",
        "/api/v1/platform/referrals/",
        body={
            "name": "Bola Placeholder",
            "email": "bola.placeholder@example.com",
            "code": MARKETER_CODE,
            "bank_code": BANK_CODE,
            "account_number": ACCOUNT_NUMBER,
            "account_name": "Bola Placeholder",
        },
        jar=admin_jar,
        expect=409,
    )
    rec.call(
        "admin-create-marketer-invalid-code",
        "Error: the code fails validation (too short and not a-z0-9-).",
        "POST",
        "/api/v1/platform/referrals/",
        body={
            "name": "Bola Placeholder",
            "email": "bola.placeholder@example.com",
            "code": "A!",
            "bank_code": BANK_CODE,
            "account_number": ACCOUNT_NUMBER,
            "account_name": "Bola Placeholder",
        },
        jar=admin_jar,
        expect=422,
    )
    rec.call(
        "admin-create-marketer-email-in-use",
        "Error: the email already belongs to a marketer (same email, different code).",
        "POST",
        "/api/v1/platform/referrals/",
        body={
            "name": "Ada Again",
            "email": MARKETER_EMAIL,
            "code": "ada-again",
            "bank_code": BANK_CODE,
            "account_number": ACCOUNT_NUMBER,
            "account_name": "Ada Placeholder",
        },
        jar=admin_jar,
        expect=409,
    )

    # --- 3. list / get
    rec.call(
        "admin-list-marketers",
        "Admin lists marketers (any platform admin may read).",
        "GET",
        "/api/v1/platform/referrals/",
        query={"limit": 50, "offset": 0},
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "admin-get-marketer",
        "Admin reads one marketer.",
        "GET",
        f"/api/v1/platform/referrals/{marketer_id}",
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "admin-get-marketer-not-found",
        "Error: unknown marketer id.",
        "GET",
        "/api/v1/platform/referrals/11111111-1111-4111-8111-111111111111",
        jar=admin_jar,
        expect=404,
    )

    # --- 4. invite token (read from Redis) and set-password
    def live_token() -> str:
        """The one-time token, read the way TokenService stores it (hash + per-marketer index)."""
        deadline = time.time() + 10
        while time.time() < deadline:
            keys = sorted(redis_client.smembers(f"marketer_tokens:{marketer_id}"))
            keys = [k for k in keys if redis_client.exists(k)]
            if keys:
                return keys[-1].split("marketer_invite:", 1)[1]
            time.sleep(0.3)
        raise RuntimeError("no marketer token appeared in Redis")

    rec.call(
        "marketer-login-before-password",
        "Login before any password is set: 401, the same body as a wrong password.",
        "POST",
        "/api/v1/auth/marketer/login",
        body={"email": MARKETER_EMAIL, "password": MARKETER_PASSWORD},
        expect=401,
    )
    invite_token = live_token()
    rec.call(
        "marketer-set-password-invalid-token",
        "Error: an invalid or expired token.",
        "POST",
        "/api/v1/auth/marketer/set-password",
        body={"token": "not-a-real-token", "new_password": MARKETER_PASSWORD},
        expect=400,
    )
    rec.call(
        "marketer-set-password",
        "Marketer redeems the one-time invite token and sets a password (does not sign in).",
        "POST",
        "/api/v1/auth/marketer/set-password",
        body={"token": invite_token, "new_password": MARKETER_PASSWORD},
        note=(
            "Email delivery is NOT exercised live. The one-time token was read straight out of "
            "Redis (set marketer_invite:<token>, indexed by marketer_tokens:<marketer id>), "
            "exactly where TokenService.create_marketer_invite_token stores it, instead of from "
            "an email link. The value is redacted here."
        ),
        expect=200,
    )
    rec.call(
        "marketer-set-password-token-reused",
        "Error: the same token cannot be used twice.",
        "POST",
        "/api/v1/auth/marketer/set-password",
        body={"token": invite_token, "new_password": MARKETER_PASSWORD},
        expect=400,
    )

    # --- 5. login variants
    rec.call(
        "marketer-login-wrong-password",
        "Error: wrong password.",
        "POST",
        "/api/v1/auth/marketer/login",
        body={"email": MARKETER_EMAIL, "password": "Wrong-Placeholder-1!"},
        expect=401,
    )
    rec.call(
        "marketer-login-unknown-email",
        "Error: unknown email. Identical status and body to the wrong-password case.",
        "POST",
        "/api/v1/auth/marketer/login",
        body={"email": "nobody.placeholder@example.com", "password": MARKETER_PASSWORD},
        expect=401,
    )
    rec.call(
        "marketer-login",
        "Marketer signs in: tenant-style HttpOnly cookies (marketer_access_token, "
        "marketer_refresh_token); the body is only {role, redirect}.",
        "POST",
        "/api/v1/auth/marketer/login",
        body={"email": MARKETER_EMAIL, "password": MARKETER_PASSWORD},
        jar=marketer_jar,
        expect=200,
    )
    rec.call(
        "admin-suspend-marketer",
        "Admin suspends the marketer (unpaid rewards move to held; there are none yet).",
        "POST",
        f"/api/v1/platform/referrals/{marketer_id}/suspend",
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "marketer-me-while-suspended",
        "A session that was valid before the suspension stops working: 401.",
        "GET",
        "/api/v1/marketer/me",
        jar=marketer_jar,
        expect=401,
    )
    rec.call(
        "marketer-login-suspended",
        "Login while suspended: 403 account_suspended (distinct from the 401 credential errors).",
        "POST",
        "/api/v1/auth/marketer/login",
        body={"email": MARKETER_EMAIL, "password": MARKETER_PASSWORD},
        expect=403,
    )
    rec.call(
        "admin-reactivate-marketer",
        "Admin reactivates the marketer.",
        "POST",
        f"/api/v1/platform/referrals/{marketer_id}/reactivate",
        jar=admin_jar,
        expect=200,
    )

    # --- 6. refresh + logout
    marketer_jar.clear()
    rec.call(
        "marketer-login-after-reactivation",
        "Login works again after reactivation.",
        "POST",
        "/api/v1/auth/marketer/login",
        body={"email": MARKETER_EMAIL, "password": MARKETER_PASSWORD},
        jar=marketer_jar,
        expect=200,
    )
    rec.call(
        "marketer-refresh",
        "Rotate the refresh token (cookie only; the body is {token_type, expires_in}).",
        "POST",
        "/api/v1/auth/marketer/refresh",
        jar=marketer_jar,
        expect=200,
    )
    old_access = marketer_jar.value("marketer_access_token")
    rec.call(
        "marketer-logout",
        "Logout revokes the session server-side and clears the cookies. Always 200.",
        "POST",
        "/api/v1/auth/marketer/logout",
        jar=marketer_jar,
        expect=200,
    )
    rec.call(
        "marketer-me-after-logout",
        "The revoked access token no longer works: 401.",
        "GET",
        "/api/v1/marketer/me",
        headers={"cookie": f"marketer_access_token={old_access}"},
        note="The revoked access token was replayed in the Cookie header by hand.",
        expect=401,
    )

    # --- 7. forgot password + reset
    rec.call(
        "marketer-forgot-password",
        "Forgot password for a real marketer: generic 200 (the mail is sent after the response).",
        "POST",
        "/api/v1/auth/marketer/forgot-password",
        body={"email": MARKETER_EMAIL},
        expect=200,
    )
    rec.call(
        "marketer-forgot-password-unknown-email",
        "Forgot password for an unknown email: the identical generic 200.",
        "POST",
        "/api/v1/auth/marketer/forgot-password",
        body={"email": "nobody.placeholder@example.com"},
        expect=200,
    )
    reset_token = live_token()
    rec.call(
        "marketer-reset-password",
        "Reset: the same set-password endpoint redeems the reset token.",
        "POST",
        "/api/v1/auth/marketer/set-password",
        body={"token": reset_token, "new_password": MARKETER_NEW_PASSWORD},
        note=(
            "Email delivery is NOT exercised live. The reset token was read from Redis (created "
            "by the background task that runs after forgot-password) instead of an email link."
        ),
        expect=200,
    )
    rec.call(
        "marketer-login-old-password",
        "Error: the old password no longer works.",
        "POST",
        "/api/v1/auth/marketer/login",
        body={"email": MARKETER_EMAIL, "password": MARKETER_PASSWORD},
        expect=401,
    )
    marketer_jar.clear()
    rec.call(
        "marketer-login-new-password",
        "Login with the new password (this session is used for the rest of the run).",
        "POST",
        "/api/v1/auth/marketer/login",
        body={"email": MARKETER_EMAIL, "password": MARKETER_NEW_PASSWORD},
        jar=marketer_jar,
        expect=200,
    )

    # --- 8. dashboard before any referral
    rec.call(
        "marketer-me",
        "The marketer's profile: name, code and the shareable signup link.",
        "GET",
        "/api/v1/marketer/me",
        jar=marketer_jar,
        expect=200,
    )
    rec.call(
        "marketer-referrals-empty",
        "No referrals yet.",
        "GET",
        "/api/v1/marketer/referrals",
        jar=marketer_jar,
        expect=200,
    )
    rec.call(
        "marketer-earnings-empty",
        "No earnings yet.",
        "GET",
        "/api/v1/marketer/earnings",
        jar=marketer_jar,
        expect=200,
    )

    # --- 9. signups
    def register(slug, description, company, person, email, code=None, source=None, expect=201):
        body = {"company_name": company, "admin_full_name": person, "admin_email": email}
        if code is not None:
            body["referral_code"] = code
        if source is not None:
            body["referral_source"] = source
        return rec.call(
            slug, description, "POST", "/api/v1/auth/register-company", body=body, expect=expect
        )

    a_resp = register(
        "signup-with-referral-code",
        "A company signs up with the marketer's code (form field).",
        "Placeholder Stores Ltd",
        "Tola Placeholder",
        "tola.placeholder@example.com",
        code=MARKETER_CODE,
        source="form",
    )
    tenant_a = a_resp.json()["tenant_id"]
    register(
        "signup-with-unknown-code",
        "A signup with an unknown code: the identical success shape (a bad code never fails or "
        "changes a signup).",
        "Placeholder Unknown Ltd",
        "Uche Placeholder",
        "uche.placeholder@example.com",
        code="no-such-code",
        source="form",
    )
    rec.call(
        "signup-company-name-taken",
        "Error: the company name already exists (a plain 400 string detail).",
        "POST",
        "/api/v1/auth/register-company",
        body={
            "company_name": "Placeholder Stores Ltd",
            "admin_full_name": "Tola Again",
            "admin_email": "tola.again@example.com",
            "referral_code": MARKETER_CODE,
        },
        expect=400,
    )

    # --- 10. the referred tenant, masked
    rec.call(
        "marketer-referrals-signed-up",
        "The referred company appears as signed_up, with a masked email.",
        "GET",
        "/api/v1/marketer/referrals",
        jar=marketer_jar,
        expect=200,
    )

    # --- 11. first payment webhook
    secret = settings.PAYSTACK_SECRET_KEY

    def insert_payment(tenant_id: str, reference: str) -> None:
        db.add(
            PaymentTransaction(
                tenant_id=UUID(tenant_id),
                reference=reference,
                amount=1_000_000,
                tier="professional",
                billing_cycle="monthly",
                status="pending",
            )
        )
        db.commit()

    def charge(reference: str) -> bytes:
        return json.dumps(
            {
                "event": "charge.success",
                "data": {
                    "reference": reference,
                    "amount": 1_000_000,
                    "currency": "NGN",
                    "channel": "card",
                    "customer": {
                        "email": "billing.placeholder@example.com",
                        "customer_code": "CUS_placeholder",
                    },
                    "authorization": {
                        "authorization_code": "AUTH_placeholder",
                        "signature": "SIG_placeholder_a",
                    },
                },
            }
        ).encode()

    ref_a = "capture-ref-placeholder-a"
    insert_payment(tenant_a, ref_a)
    body_a = charge(ref_a)
    rec.call(
        "webhook-invalid-signature",
        "Error: a webhook with a wrong signature is refused (400) and changes nothing.",
        "POST",
        "/api/v1/webhooks/paystack",
        raw_content=body_a,
        headers={"x-paystack-signature": "0" * 128},
        redact_request_headers=("x-paystack-signature",),
        expect=400,
    )
    note_hook = (
        "The pending PaymentTransaction was inserted directly into the scratch database (the "
        "normal checkout, POST /api/v1/subscription/upgrade/initialize, calls the real Paystack "
        "API). The payload is shaped from what the handler reads (reference, channel, customer, "
        "authorization); it is NOT a captured Paystack delivery. It was signed with the same "
        "HMAC-SHA512 the endpoint verifies, using the local test PAYSTACK_SECRET_KEY."
    )
    rec.call(
        "webhook-charge-success-first-payment",
        "The first Paystack charge.success of the referred tenant: subscription upgraded and the "
        "referral reward created (nothing in the response says so; read it via the marketer API).",
        "POST",
        "/api/v1/webhooks/paystack",
        raw_content=body_a,
        headers={"x-paystack-signature": sign(secret, body_a)},
        redact_request_headers=("x-paystack-signature",),
        note=note_hook,
        expect=200,
    )
    rec.call(
        "webhook-charge-success-replay",
        "Paystack redelivers the same event: already_processed, no second reward.",
        "POST",
        "/api/v1/webhooks/paystack",
        raw_content=body_a,
        headers={"x-paystack-signature": sign(secret, body_a)},
        redact_request_headers=("x-paystack-signature",),
        note=note_hook,
        expect=200,
    )

    # --- 12. after conversion
    rec.call(
        "marketer-referrals-converted",
        "The referral is now converted.",
        "GET",
        "/api/v1/marketer/referrals",
        jar=marketer_jar,
        expect=200,
    )
    rec.call(
        "marketer-earnings-pending",
        "Earnings after conversion: the reward is pending (inside the hold period).",
        "GET",
        "/api/v1/marketer/earnings",
        jar=marketer_jar,
        expect=200,
    )

    # --- 13. fraud-held rewards via a shared domain
    tenants = {}
    rec.call(
        "signup-rate-limited",
        "Error: the per-IP signup budget (3 registrations per hour) is exhausted: 429.",
        "POST",
        "/api/v1/auth/register-company",
        body={
            "company_name": "Placeholder Traders Ltd",
            "admin_full_name": "Bisi Placeholder",
            "admin_email": "bisi.placeholder@example.com",
            "referral_code": MARKETER_CODE,
            "referral_source": "form",
        },
        note="The three earlier signup calls from this IP used up the hourly budget.",
        expect=429,
    )
    for key in redis_client.scan_iter("http_registration*"):
        redis_client.delete(key)  # so the run can create the next companies from one IP
    for key, company, person, email in (
        ("b", "Placeholder Traders Ltd", "Bisi Placeholder", "bisi.placeholder@example.com"),
        ("c", "Placeholder Mart Ltd", "Chidi Placeholder", "chidi.placeholder@example.com"),
    ):
        response = register(
            f"signup-referred-tenant-{key}",
            f"Another referred company ({company}) signs up with the same code.",
            # the registration budget in Redis was cleared by the script just before
            company,
            person,
            email,
            code=MARKETER_CODE,
            source="form",
        )
        tenants[key] = response.json()["tenant_id"]

    register(
        "signup-self-referral-same-email-domain",
        "A signup whose admin email shares the marketer's email domain: the same 201 shape, but "
        "it is treated as a self-referral (never rewarded, hidden from the marketer's list).",
        "Placeholder Partners Ltd",
        "Dayo Placeholder",
        "dayo.placeholder@example.org",
        code=MARKETER_CODE,
        source="form",
    )
    for tenant_id in (tenant_a, tenants["b"], tenants["c"]):
        db.execute(
            text("UPDATE tenants SET widget_last_seen_host = :h WHERE id = :t"),
            {"h": SHARED_HOST, "t": tenant_id},
        )
    db.commit()
    for key, number in (("b", "b"), ("c", "c")):
        reference = f"capture-ref-placeholder-{number}"
        insert_payment(tenants[key], reference)
        payload = charge(reference).replace(
            b"SIG_placeholder_a", f"SIG_placeholder_{number}".encode()
        )
        rec.call(
            f"webhook-charge-success-tenant-{key}",
            f"First payment of the referred tenant {key.upper()}, which shares a website domain "
            "with another referred tenant: the reward is created held.",
            "POST",
            "/api/v1/webhooks/paystack",
            raw_content=payload,
            headers={"x-paystack-signature": sign(secret, payload)},
            redact_request_headers=("x-paystack-signature",),
            note=(
                note_hook
                + " The shared domain was set by writing tenants.widget_last_seen_host in the "
                "scratch database for the three referred tenants (the widget normally records "
                "it when it loads on the customer's site)."
            ),
            expect=200,
        )

    queue = rec.call(
        "admin-rewards-held-queue",
        "The review queue (status=held): reasons and the other tenants sharing the domain.",
        "GET",
        "/api/v1/platform/referrals/rewards",
        query={"status": "held"},
        jar=admin_jar,
        expect=200,
    )
    held = {r["tenant_company_name"]: r["id"] for r in queue.json()["items"]}
    release_id = held["Placeholder Traders Ltd"]
    void_id = held["Placeholder Mart Ltd"]
    rec.call(
        "admin-rewards-all",
        "The same queue with status=all (every status).",
        "GET",
        "/api/v1/platform/referrals/rewards",
        query={"status": "all"},
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "admin-release-reward-note-missing",
        "Error: a note is required to release a reward.",
        "POST",
        f"/api/v1/platform/referrals/rewards/{release_id}/release",
        body={"note": "   "},
        jar=admin_jar,
        expect=422,
    )
    rec.call(
        "admin-release-reward-plain-admin",
        "Role matrix: a plain ADMIN cannot release (403).",
        "POST",
        f"/api/v1/platform/referrals/rewards/{release_id}/release",
        body={"note": "Checked the domain evidence."},
        jar=plain_jar,
        expect=403,
    )
    rec.call(
        "admin-release-reward",
        "Super admin releases a held reward after review.",
        "POST",
        f"/api/v1/platform/referrals/rewards/{release_id}/release",
        body={"note": "Checked the domain evidence: unrelated businesses."},
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "admin-release-reward-again",
        "Error: releasing a reward that is no longer held (409 reward_state_invalid).",
        "POST",
        f"/api/v1/platform/referrals/rewards/{release_id}/release",
        body={"note": "Second attempt."},
        jar=admin_jar,
        expect=409,
    )
    rec.call(
        "admin-void-reward",
        "Super admin voids the other held reward.",
        "POST",
        f"/api/v1/platform/referrals/rewards/{void_id}/void",
        body={"note": "Same owner as another referred company."},
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "admin-release-reward-not-found",
        "Error: unknown reward id.",
        "POST",
        "/api/v1/platform/referrals/rewards/11111111-1111-4111-8111-111111111111/release",
        body={"note": "Does not exist."},
        jar=admin_jar,
        expect=404,
    )

    # --- 14. promote pending (job function, after the hold)
    future = datetime.now(UTC) + timedelta(days=8)
    promoted = promote_pending(db, future)
    db.commit()
    rec.call(
        "marketer-earnings-payable",
        "Earnings after the 7-day hold has passed: the reward is payable.",
        "GET",
        "/api/v1/marketer/earnings",
        jar=marketer_jar,
        note=(
            "Not an HTTP step: app.services.referral.rewards.promote_pending(db, now) was called "
            f"once with now = real now + 8 days and committed (it promoted {promoted} reward(s)). "
            "This is the function the daily 03:00 UTC job calls; the HTTP call here only reads "
            "the result."
        ),
        expect=200,
    )

    # --- 15. batch prepare / approve / mark paid
    batch = prepare_batch(db, future)
    batch_id = str(batch.id) if batch else None
    db.commit()
    if batch_id is None:
        print("!! no payable reward, no batch prepared")
        return 1
    note_batch = (
        "Not an HTTP step: app.services.referral.payouts.prepare_batch(db, now) was called once "
        "with now = real now + 8 days (the function the 1st/15th 04:00 UTC job calls) and "
        "committed, which created this draft batch. The period label therefore carries that "
        "shifted date."
    )
    rec.call(
        "admin-batches-list",
        "Payout batches, newest first.",
        "GET",
        "/api/v1/platform/referrals/batches",
        jar=admin_jar,
        note=note_batch,
        expect=200,
    )
    detail = rec.call(
        "admin-batch-detail-draft",
        "One draft batch with its items (bank_last4 only) and current_amount_kobo per item.",
        "GET",
        f"/api/v1/platform/referrals/batches/{batch_id}",
        jar=admin_jar,
        expect=200,
    )
    item_marketer = detail.json()["items"][0]["marketer_id"]
    current_amount = detail.json()["items"][0]["current_amount_kobo"]
    rec.call(
        "admin-batch-approve-plain-admin",
        "Role matrix: a plain ADMIN cannot approve (403).",
        "POST",
        f"/api/v1/platform/referrals/batches/{batch_id}/approve",
        jar=plain_jar,
        expect=403,
    )
    rec.call(
        "admin-batch-approve",
        "Super admin approves the batch. Default (manual) provider: items become awaiting_manual "
        "and nothing is sent anywhere.",
        "POST",
        f"/api/v1/platform/referrals/batches/{batch_id}/approve",
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "admin-batch-approve-again",
        "Approving again is idempotent: 200 with the batch unchanged.",
        "POST",
        f"/api/v1/platform/referrals/batches/{batch_id}/approve",
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "admin-batch-mark-paid-wrong-amount",
        "Error: the amount the admin says they paid differs from the item's current amount "
        "(409 amount_mismatch, with current_amount_kobo). Nothing changes.",
        "POST",
        f"/api/v1/platform/referrals/batches/{batch_id}/mark-paid",
        body={
            "items": [
                {
                    "marketer_id": item_marketer,
                    "bank_reference": "BANK-REF-PLACEHOLDER-001",
                    "amount_kobo": current_amount - 1,
                }
            ]
        },
        jar=admin_jar,
        expect=409,
    )
    rec.call(
        "admin-batch-mark-paid",
        "Admin records the manual bank transfer with the correct amount: item and batch paid.",
        "POST",
        f"/api/v1/platform/referrals/batches/{batch_id}/mark-paid",
        body={
            "items": [
                {
                    "marketer_id": item_marketer,
                    "bank_reference": "BANK-REF-PLACEHOLDER-001",
                    "amount_kobo": current_amount,
                }
            ]
        },
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "admin-batch-mark-paid-again",
        "Error: marking the same batch paid again (409, the batch is no longer open).",
        "POST",
        f"/api/v1/platform/referrals/batches/{batch_id}/mark-paid",
        body={
            "items": [
                {
                    "marketer_id": item_marketer,
                    "bank_reference": "BANK-REF-PLACEHOLDER-002",
                    "amount_kobo": current_amount,
                }
            ]
        },
        jar=admin_jar,
        expect=409,
    )
    rec.call(
        "admin-batch-detail-paid",
        "The batch after payment.",
        "GET",
        f"/api/v1/platform/referrals/batches/{batch_id}",
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "admin-batch-not-found",
        "Error: unknown batch id.",
        "GET",
        "/api/v1/platform/referrals/batches/11111111-1111-4111-8111-111111111111",
        jar=admin_jar,
        expect=404,
    )

    # --- 16. earnings after payout
    rec.call(
        "marketer-earnings-after-payout",
        "Earnings after the payout: paid_total and the payouts history list.",
        "GET",
        "/api/v1/marketer/earnings",
        jar=marketer_jar,
        expect=200,
    )
    rec.call(
        "marketer-referrals-final",
        "Referrals at the end of the run: three referred companies; the unknown-code and "
        "self-referral signups are not listed.",
        "GET",
        "/api/v1/marketer/referrals",
        jar=marketer_jar,
        expect=200,
    )

    # --- 17. config
    rec.call(
        "admin-config-get",
        "Effective programme settings and where each value comes from (env or admin override).",
        "GET",
        "/api/v1/platform/referrals/config",
        jar=admin_jar,
        expect=200,
    )
    rec.call(
        "admin-config-patch-out-of-bounds",
        "Error: a value outside the allowed bounds (422).",
        "PATCH",
        "/api/v1/platform/referrals/config",
        body={"reward_kobo": 1},
        jar=admin_jar,
        expect=422,
    )
    rec.call(
        "admin-config-patch",
        "Super admin changes the hold period (affects rewards created afterwards only).",
        "PATCH",
        "/api/v1/platform/referrals/config",
        body={"hold_days": 14},
        jar=admin_jar,
        expect=200,
    )

    # --- 18. role matrix
    rec.call(
        "role-plain-admin-create-marketer",
        "Role matrix: a plain ADMIN cannot create a marketer (403).",
        "POST",
        "/api/v1/platform/referrals/",
        body={
            "name": "Cy Placeholder",
            "email": "cy.placeholder@example.com",
            "code": "cy-placeholder",
            "bank_code": BANK_CODE,
            "account_number": ACCOUNT_NUMBER,
            "account_name": "Cy Placeholder",
        },
        jar=plain_jar,
        expect=403,
    )
    rec.call(
        "role-plain-admin-list-marketers",
        "Role matrix: a plain ADMIN can read.",
        "GET",
        "/api/v1/platform/referrals/",
        jar=plain_jar,
        expect=200,
    )
    rec.call(
        "role-no-auth-platform",
        "Role matrix: no credentials on a platform route (401).",
        "GET",
        "/api/v1/platform/referrals/",
        jar=anon,
        expect=401,
    )
    rec.call(
        "role-no-auth-marketer-me",
        "Role matrix: no credentials on a marketer route (401).",
        "GET",
        "/api/v1/marketer/me",
        jar=anon,
        expect=401,
    )
    tenant_user = db.scalar(select(User).where(User.tenant_id == UUID(tenant_a)))
    tenant_token = create_access_token(
        subject=str(tenant_user.id),
        additional_claims={"role": tenant_user.role.value, "tenant_id": tenant_a},
    )
    rec.call(
        "role-tenant-token-on-marketer-me",
        "Role matrix: a tenant admin's token on /marketer/me (401).",
        "GET",
        "/api/v1/marketer/me",
        headers={
            "authorization": f"Bearer {tenant_token}",
            "cookie": f"access_token={tenant_token}",
        },
        note=(
            "The tenant token was minted with the project's create_access_token for the signed-up "
            "tenant's admin user (a real tenant login needs the temporary password that is only "
            "emailed). It is sent both as a cookie and as a Bearer header."
        ),
        expect=401,
    )
    marketer_token = marketer_jar.value("marketer_access_token")
    rec.call(
        "role-marketer-token-on-platform-route",
        "Role matrix: a marketer's token on a platform route (401).",
        "GET",
        "/api/v1/platform/referrals/",
        headers={"authorization": f"Bearer {marketer_token}"},
        expect=401,
    )

    # --- sanity: the fake bank account must never be echoed back
    leaks = []
    for path in sorted(out_dir.glob("*.json")):
        data = json.loads(path.read_text())
        if ACCOUNT_NUMBER in json.dumps(data["response"]):
            leaks.append(path.name)
    batch_status = db.scalar(select(PayoutBatch.status).where(PayoutBatch.id == UUID(batch_id)))
    print(f"batch {batch_id}: {batch_status}")
    if leaks:
        print(f"!! bank account number appears in responses: {leaks}")
    write_index(out_dir, rec)
    if rec.mismatches or leaks:
        print("\nUnexpected statuses:")
        for line in rec.mismatches:
            print("  " + line)
        return 1
    print(f"\n{len(rec.index)} captures written to {out_dir}")
    return 0


# --------------------------------------------------------------------------------- INDEX.md

HOW_RUN = """\
## How the app was run

A scratch Postgres database (`ref_capture`, migrated with `alembic upgrade head`) and Redis logical
database 5; uvicorn on 127.0.0.1:18765 started by hand with a throwaway Fernet `ENCRYPTION_KEY` and
`ENVIRONMENT=staging` (the app's relaxed per-IP rate limits for auth endpoints; the signup limit of
3 per hour per IP is the same in every environment, so the script clears that one Redis counter once,
right after capturing the 429). SMTP pointed at an unused local port: every send fails fast and is
logged, no email leaves the machine. Qdrant and OpenAI were never needed by these flows.

## Easy to miss (all visible in the captures)

- Cookie names for the marketer session are `marketer_access_token` (path `/`) and
  `marketer_refresh_token` (path `/api/v1/auth/marketer`); both are `HttpOnly; Secure; SameSite=lax`.
  Login returns only `{"role": "marketer", "redirect": "/marketer"}`.
- Error bodies come in three shapes: `{"detail": {"code", "message"}}` (domain errors),
  `{"detail": "<string>"}` (auth failures, the signup 400) and the FastAPI 422 list. The webhook uses
  `{"error": "..."}`. The 429 is `{"detail": {"error", "limit", "window_seconds",
  "retry_after_seconds"}}`.
- A signup whose admin email shares the marketer's (non-webmail) email domain is a self-referral:
  same 201, never listed for the marketer, never rewarded. The captures use `@example.org` for the
  marketer for that reason.
- The marketer's `/referrals` shows a referral whose reward was **voided** as `signed_up` (with
  `trial_days_left`), even though the tenant paid.
- Rewards created from signups that share one IP and user agent also carry the `shared_origin`
  reason, besides `shared_domain`.
- `GET /marketer/me` `link` is built from `REFERRAL_LINK_BASE_URL`, not from `FRONTEND_URL`.
- `PATCH /config` returns `source_by_key` (`env` or `db`); the changed value only affects rewards
  created afterwards.
"""

NOT_EXERCISED = """\
## Not exercised live

These were NOT run against anything real, and the captures say nothing about them. Anything the
frontend guide states about them has to be labelled as coming from code or docs, not from a capture.

- **Paystack Transfers provider against the real API**: recipient creation, `initiate_transfer`,
  `verify_transfer` and the `transfer.success` / `transfer.failed` / `transfer.reversed` webhooks.
  The payout was exercised with the default **manual** provider only (items become
  `awaiting_manual`, an admin records the bank transfer with `mark-paid`). No real or test
  Paystack API call was made at all.
- **OTP-required transfers** (a Paystack account that requires an OTP to finalise API transfers).
- **The Paystack dispute webhook** (`charge.dispute.create`) payload shape: it is read defensively
  from public docs and was never captured from Paystack. A release/void refused with 409
  `reward_in_flight` (a held reward still tied to an in-flight transfer) needs a Paystack transfer
  in flight, so it is not reproduced either.
- **Email delivery** of all four marketer emails (invite, password reset, conversion notice and
  payout notice). SMTP pointed at an unused local port; sends fail fast and are logged. The
  one-time invite and reset tokens were read out of Redis instead of from an email link.
- **The real checkout** (`POST /api/v1/subscription/upgrade/initialize`): it calls the Paystack
  API. The pending `PaymentTransaction` rows were inserted directly and the `charge.success`
  payload is shaped from what the handler reads, not a captured Paystack delivery.
- **The `luran_ref` cookie and the frontend**: capturing the referral code from a link, the cookie
  itself and the signup form are frontend work. These captures send `referral_code` (and
  `referral_source`) in the signup body only.
- **A tenant login**: the tenant token used for the 401 sample was minted with the project's token
  function, because a real tenant login needs the temporary password that is only emailed.
- **The scheduled jobs themselves** (arq cron): the promote and prepare-batch *functions* were
  called once from the capture script with a shifted `now`; the cron registration was not run.
- **Browser behaviour** (CORS, Secure cookies in a real browser): the cookies carry `Secure`, so
  the script keeps its own cookie jar and sends the `Cookie` header explicitly over plain http.
"""


def write_index(out_dir: Path, rec: Recorder) -> None:
    lines = [
        "# Referral program: live captures",
        "",
        "Real requests and responses from the referral backend, recorded by "
        "`scripts/capture_referral.py` against a locally running app on a scratch Postgres "
        "database and Redis (nothing real: no production, no Paystack, no email).",
        "",
        "Each file is `{description, note?, request, response, redacted?}`. Bodies are verbatim. "
        'Only secrets are replaced with `"<redacted>"` (token and cookie values, the webhook '
        "signature); request headers keep only `content-type` and the NAMES of the cookies sent "
        "(`cookies_sent`). All names, emails, the bank account (`0000000000`) and bank code "
        "(`000`) are obvious placeholders. IDs and timestamps are the scratch database's own.",
        "",
        "Where a step did not go over HTTP (job functions, reading a token from Redis, inserting "
        "the pending payment), the capture's `note` says so.",
        "",
        "## Captures",
        "",
        "| File | Call | Status | What it shows |",
        "|---|---|---|---|",
    ]
    for file_name, description, call, status in rec.index:
        lines.append(f"| `{file_name}` | `{call}` | {status} | {description} |")
    lines += ["", HOW_RUN, NOT_EXERCISED]
    (out_dir / "INDEX.md").write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
