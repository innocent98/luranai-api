"""Canonical sales intent labels and helpers.

Dependency-free by design — safe to import from models, services, and tests
without risking circular imports.
"""

import json

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


# ---------------------------------------------------------------------------
# Structured-output schema (Task 2)
# ---------------------------------------------------------------------------

SALES_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string"},
        "intent": {
            "type": "string",
            "enum": sorted(SALES_INTENTS),
        },
        "signals": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "asked_features": {"type": "boolean"},
                "requested_demo": {"type": "boolean"},
                "mentioned_competitor": {"type": "boolean"},
                "ready_to_buy": {"type": "boolean"},
            },
            "required": ["asked_features", "requested_demo", "mentioned_competitor", "ready_to_buy"],
        },
        "captured": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": ["string", "null"]},
                "email": {"type": ["string", "null"]},
                "company": {"type": ["string", "null"]},
                "team_size": {"type": ["string", "null"]},
                "industry": {"type": ["string", "null"]},
            },
            "required": ["name", "email", "company", "team_size", "industry"],
        },
    },
    "required": ["reply", "intent", "signals", "captured"],
}

_FALLBACK_QUESTION = "What are you hoping to achieve?"


def _extract_question(item: dict | str) -> str:
    """Return the question text from a dict item or plain string."""
    if isinstance(item, dict):
        return item.get("question") or _FALLBACK_QUESTION
    if isinstance(item, str) and item.strip():
        return item.strip()
    return _FALLBACK_QUESTION


def build_sales_directive(qualification_questions: list) -> str:
    """Return a system-prompt addendum for sales-intent conversations.

    Instructs the model to:
    1. Answer the prospect's question normally.
    2. Ask exactly ONE qualifying question (taken from the first configured
       question; falls back to a default if the list is empty or malformed).
    3. Once the prospect answers, request name + email conversationally with
       the phrase "no forms — just reply here".
    4. Never pitch on a SUPPORT intent.
    5. Extract only stated facts into ``captured`` / ``signals`` — no hallucination.
    """
    if qualification_questions:
        first_question = _extract_question(qualification_questions[0])
    else:
        first_question = _FALLBACK_QUESTION

    return (
        "## Sales Engagement Directive\n\n"
        "When the conversation involves a sales intent (e.g. pricing, demo, purchase):\n\n"
        f"1. Answer the prospect's question clearly and helpfully.\n"
        f"2. Ask exactly one qualifying question: \"{first_question}\"\n"
        "3. Once the prospect has answered, ask for their name and email "
        "conversationally — no forms — just reply here.\n"
        "4. Never sell or upsell when the intent is SUPPORT; focus solely on "
        "resolving the support issue.\n"
        "5. Populate `captured` and `signals` only with facts the prospect has "
        "explicitly stated — do not infer or hallucinate missing values.\n"
    )


_REQUIRED_KEYS: frozenset[str] = frozenset({"reply", "intent", "signals", "captured"})


def parse_sales_output(raw: str) -> dict | None:
    """Parse and validate a model's structured JSON response.

    Returns a dict with keys ``{reply, intent, signals, captured}`` on success,
    or ``None`` if the input is not valid JSON or is missing required keys.
    ``intent`` is normalised via :func:`normalize_intent`.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    if not _REQUIRED_KEYS.issubset(data.keys()):
        return None

    return {
        "reply": data["reply"],
        "intent": normalize_intent(data["intent"]),
        "signals": data["signals"],
        "captured": data["captured"],
    }
