"""Bank list and account-name lookup for the referral admin forms, backed by Paystack.

The admin picks a bank by name; the form sends the code Paystack returned. The list is the same
for every caller and not secret, so it is cached per process for 24 hours. If a refresh fails the
last copy is served (flagged ``stale``) for up to 7 days; with nothing cached the caller gets
``BankProviderUnavailable`` and the form falls back to manual entry.

Nothing here logs an account number, a Paystack message (it can echo the account number) or the
Authorization header: failures are logged by HTTP status only.
"""

import threading
import time
from dataclasses import dataclass

from loguru import logger

from app.services.paystack_service import PaystackService, PaystackTransferError

BANK_CACHE_TTL_SECONDS = 24 * 3600
BANK_STALE_GRACE_SECONDS = 7 * 24 * 3600

# Paystack answers these for "that account does not resolve" (documented: 422).
_NOT_RESOLVED_STATUSES = {400, 404, 422}


class BankLookupError(Exception):
    code = "bank_lookup_failed"


class BankProviderUnavailable(BankLookupError):
    """Paystack is unreachable, refused our key, throttled us, or sent something unreadable."""

    code = "bank_provider_unavailable"


class AccountNotResolved(BankLookupError):
    """Paystack answered, and has no account for that bank code and number."""

    code = "account_not_resolved"


@dataclass(frozen=True)
class BankList:
    banks: list[dict[str, str]]
    stale: bool
    test_mode: bool


_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def _now() -> float:
    return time.monotonic()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _is_test_mode(paystack: PaystackService) -> bool:
    return paystack.secret_key.startswith("sk_test_")


def _normalise(rows: list[dict]) -> list[dict[str, str]]:
    """Active NUBAN banks as ``{name, code}``, sorted by name, one row per code."""
    seen: set[str] = set()
    banks: list[dict[str, str]] = []
    for row in rows:
        name, code = row.get("name"), row.get("code")
        if not isinstance(name, str) or not isinstance(code, str) or not name.strip() or not code:
            continue
        if row.get("active") is not True or row.get("is_deleted") is True:
            continue
        if row.get("type") not in (None, "nuban"):
            continue
        if code in seen:
            continue
        seen.add(code)
        banks.append({"name": name.strip(), "code": code.strip()})
    banks.sort(key=lambda b: (b["name"].casefold(), b["code"]))
    return banks


def get_banks(paystack: PaystackService, country: str = "nigeria") -> BankList:
    test_mode = _is_test_mode(paystack)
    now = _now()
    with _lock:
        cached = _cache.get(country)
    if cached and now - cached[0] < BANK_CACHE_TTL_SECONDS:
        return BankList(cached[1], stale=False, test_mode=test_mode)
    try:
        banks = _normalise(paystack.list_banks(country))
        if not banks:
            raise PaystackTransferError("Paystack returned no active banks", definitive=False)
    except PaystackTransferError as exc:
        logger.warning("Paystack bank list failed (status {})", exc.status_code)
        if cached and now - cached[0] < BANK_STALE_GRACE_SECONDS:
            return BankList(cached[1], stale=True, test_mode=test_mode)
        raise BankProviderUnavailable("Paystack bank list unavailable") from None
    with _lock:
        _cache[country] = (now, banks)
    return BankList(banks, stale=False, test_mode=test_mode)


def resolve_account(paystack: PaystackService, *, bank_code: str, account_number: str) -> str:
    try:
        return paystack.resolve_account(account_number=account_number, bank_code=bank_code)
    except PaystackTransferError as exc:
        if exc.status_code in _NOT_RESOLVED_STATUSES and exc.definitive:
            raise AccountNotResolved("Account could not be resolved") from None
        logger.warning("Paystack account resolve failed (status {})", exc.status_code)
        raise BankProviderUnavailable("Paystack account lookup unavailable") from None
