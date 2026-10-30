#!/usr/bin/env python
"""Drive a LOCALLY RUNNING Luran API through the referral bank picker and save the real traffic.

Same capture format and safety guard as ``scripts/capture_referral.py`` (it reuses its recorder):
a scratch database ``ref_capture`` and Redis logical database 5, and an app started with
``PAYSTACK_BASE_URL`` pointing at ``scripts/fake_paystack.py``. The Paystack side is a FAKE: the
captures prove Luran's endpoints, not Paystack's real answers.

    # terminal 1: python scripts/fake_paystack.py 18790
    # terminal 2: PAYSTACK_BASE_URL=http://127.0.0.1:18790 PAYSTACK_SECRET_KEY=sk_test_<fake> \
    #             uvicorn app.main:app --port 18765        (scratch DATABASE_URL / REDIS_URL)
    # terminal 3: python scripts/capture_bank_picker.py --fake-paystack http://127.0.0.1:18790

Start the app with a fresh process so the bank cache is empty: the first capture shows the
"Paystack down" answer.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.capture_referral import (  # noqa: E402
    ADMIN_PASSWORD,
    PLAIN_ADMIN_EMAIL,
    SUPER_EMAIL,
    Jar,
    Recorder,
    _guard_environment,
)

OUT = REPO / "docs" / "fe-integration-captures" / "referral-bank-picker"
BASE = "/api/v1/platform/referrals"
PLACEHOLDER_ACCOUNT = "0000000000"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--fake-paystack", default="http://127.0.0.1:18790")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    _guard_environment()

    from app.db.models.platform_admin import PlatformAdminRole
    from app.db.session import SessionLocal
    from app.schemas.platform_admin import PlatformAdminCreate
    from app.services.platform_admin_auth_service import PlatformAdminAuthService

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.json"):
        stale.unlink()
    rec = Recorder(args.base_url, out)
    fake = httpx.Client(base_url=args.fake_paystack, timeout=10)

    db = SessionLocal()
    db.get_bind().echo = False
    for email, role in (
        (SUPER_EMAIL, PlatformAdminRole.SUPER_ADMIN),
        (PLAIN_ADMIN_EMAIL, PlatformAdminRole.ADMIN),
    ):
        try:
            PlatformAdminAuthService(db).create_admin(
                PlatformAdminCreate(
                    email=email, password=ADMIN_PASSWORD, full_name="Placeholder", role=role
                )
            )
        except ValueError:
            pass
    db.commit()
    import redis

    redis_client = redis.Redis.from_url(os.environ["REDIS_URL"])

    def reset_rate_limits() -> None:
        # Scratch Redis only (guarded above): the admin login budget is 3 per 15 minutes per IP.
        for key in redis_client.scan_iter(match="http_*"):
            redis_client.delete(key)

    super_jar, plain_jar = Jar(), Jar()
    for slug, jar, email in (
        ("super-admin-login", super_jar, SUPER_EMAIL),
        ("plain-admin-login", plain_jar, PLAIN_ADMIN_EMAIL),
    ):
        reset_rate_limits()
        rec.call(
            slug,
            "Platform admin signs in (cookies).",
            "POST",
            "/api/v1/platform/auth/login",
            body={"email": email, "password": ADMIN_PASSWORD},
            jar=jar,
            expect=200,
            note="Admin row created beforehand with PlatformAdminAuthService.create_admin on the scratch database.",
        )

    reset_rate_limits()
    paystack_down = "The fake Paystack was switched to mode 'down' (HTTP 503 on /bank); the app's bank cache was empty."
    fake.post("/__mode", json={"mode": "down"})
    rec.call(
        "banks-paystack-down-no-cache",
        "Bank list while Paystack is down and nothing is cached.",
        "GET",
        f"{BASE}/banks",
        jar=super_jar,
        expect=503,
        note=paystack_down,
    )
    fake.post("/__mode", json={"mode": "up"})
    rec.call(
        "banks-ok",
        "Bank list: active banks only, {name, code}, sorted by name, fetched over two Paystack pages.",
        "GET",
        f"{BASE}/banks",
        jar=super_jar,
        expect=200,
        note="Fake Paystack in mode 'up'. test_mode is true because the app runs with a sk_test_ placeholder key.",
    )
    fake.post("/__mode", json={"mode": "down"})
    rec.call(
        "banks-cached-while-paystack-down",
        "Second call is served from the 24 h cache although Paystack is down again.",
        "GET",
        f"{BASE}/banks",
        jar=super_jar,
        expect=200,
        note="Fake Paystack switched back to 'down' before this call: the answer proves the cache.",
    )
    fake.post("/__mode", json={"mode": "up"})
    rec.call(
        "banks-plain-admin",
        "A plain ADMIN may read the bank list.",
        "GET",
        f"{BASE}/banks",
        jar=plain_jar,
        expect=200,
    )
    rec.call("banks-no-auth", "No credentials.", "GET", f"{BASE}/banks", expect=401)

    def resolve(slug, desc, number, expect, jar=super_jar, body=None, note=None):
        rec.call(
            slug,
            desc,
            "POST",
            f"{BASE}/banks/resolve",
            body=body if body is not None else {"bank_code": "058", "account_number": number},
            jar=jar,
            expect=expect,
            note=note,
        )

    resolve(
        "resolve-ok", "Account resolves: only the holder name comes back.", PLACEHOLDER_ACCOUNT, 200
    )
    resolve(
        "resolve-not-found",
        "Paystack says no such account (HTTP 422 from the fake).",
        "0000000001",
        422,
    )
    resolve("resolve-paystack-error", "Paystack answers 500.", "0000000002", 503)
    resolve("resolve-paystack-throttled", "Paystack answers 429.", "0000000003", 503)
    resolve(
        "resolve-bad-account-number",
        "Account number is not 10 digits (rejected before any Paystack call).",
        "123",
        422,
    )
    resolve(
        "resolve-bad-bank-code",
        "Bank code empty.",
        None,
        422,
        body={"bank_code": "", "account_number": PLACEHOLDER_ACCOUNT},
    )
    resolve(
        "resolve-plain-admin",
        "A plain ADMIN may not resolve accounts.",
        PLACEHOLDER_ACCOUNT,
        403,
        jar=plain_jar,
    )
    rec.call(
        "resolve-no-auth",
        "No credentials.",
        "POST",
        f"{BASE}/banks/resolve",
        body={"bank_code": "058", "account_number": PLACEHOLDER_ACCOUNT},
        expect=401,
    )
    # budget is 10 per minute per admin outside staging/development; 5 calls used above (the 403 and 401 do not count)
    statuses = []
    for n in range(8):
        r = rec.call(
            f"resolve-rate-limit-{n + 1}",
            "Resolve again until the per-admin budget (10 per minute) runs out.",
            "POST",
            f"{BASE}/banks/resolve",
            body={"bank_code": "058", "account_number": PLACEHOLDER_ACCOUNT},
            jar=super_jar,
        )
        statuses.append(r.status_code)
    print("rate-limit sequence:", statuses)

    (out / "INDEX.md").write_text(
        "# Referral bank picker: live captures (FAKE Paystack)\n\n"
        "Recorded by `scripts/capture_bank_picker.py` against a locally running app whose "
        "`PAYSTACK_BASE_URL` pointed at `scripts/fake_paystack.py`. **Luran's real endpoints "
        "were exercised; Paystack was not.** The fake follows Paystack's public docs, so these "
        "files prove Luran's wire contract, not Paystack's real behaviour.\n\n"
        "| File | Call | Status | What it shows |\n|---|---|---|---|\n"
        + "\n".join(f"| `{f}` | `{c}` | {s} | {d} |" for f, d, c, s in rec.index)
        + "\n"
    )
    if rec.mismatches:
        print("MISMATCHES:", *rec.mismatches, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
