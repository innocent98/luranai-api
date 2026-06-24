"""Canonical sales intent labels and helpers.

Dependency-free by design — safe to import from models, services, and tests
without risking circular imports.
"""

SALES_INTENTS: set[str] = {
    "SUPPORT",
    "PRICING_INQUIRY",
    "ENTERPRISE_PROSPECT",
    "DEMO_REQUEST",
    "PURCHASE_INTENT",
    "OBJECTION",
    "CHURN_RISK",
}

TRIGGER_DEFAULTS: list[str] = [
    "PRICING_INQUIRY",
    "DEMO_REQUEST",
    "PURCHASE_INTENT",
    "ENTERPRISE_PROSPECT",
]

# Maps removed/renamed labels to their canonical replacements.
_LEGACY: dict[str, str] = {
    "PRODUCT_DEMO_REQUEST": "DEMO_REQUEST",
    "LEAD_QUALIFICATION": "PURCHASE_INTENT",
}


def normalize_intent(value: str | None) -> str:
    """Return the canonical intent label for *value*.

    - Strips whitespace and uppercases.
    - Remaps legacy labels via ``_LEGACY``.
    - Returns ``"SUPPORT"`` for ``None``, empty strings, or unrecognised values.
    """
    if not value:
        return "SUPPORT"
    v = value.strip().upper()
    v = _LEGACY.get(v, v)
    return v if v in SALES_INTENTS else "SUPPORT"


def is_trigger_intent(intent: str, trigger_intents: list[str]) -> bool:
    """Return ``True`` if *intent* (after normalisation) is in *trigger_intents*."""
    norm = {normalize_intent(t) for t in (trigger_intents or [])}
    return normalize_intent(intent) in norm
