"""Canonical sales intent labels and helpers.

Dependency-free by design — safe to import from models, services, and tests
without risking circular imports.
"""

import copy
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
                "offer_meeting": {"type": "boolean"},
                "offer_quote": {"type": "boolean"},
            },
            "required": [
                "asked_features",
                "requested_demo",
                "mentioned_competitor",
                "ready_to_buy",
                "offer_meeting",
                "offer_quote",
            ],
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
                "plan_interest": {"type": ["string", "null"]},
            },
            "required": ["name", "email", "company", "team_size", "industry", "plan_interest"],
        },
    },
    "required": ["reply", "intent", "signals", "captured"],
}

# Catalog variant (offerings spec 7.4): the same keys, and `signals` gains the three catalog
# fields. A separate object so the schema above, which every existing tenant is sent, never
# changes. OpenAI strict mode: every property required, no extra keys, no min/max keywords.
SALES_RESPONSE_SCHEMA_OFFERINGS: dict = copy.deepcopy(SALES_RESPONSE_SCHEMA)
_OFFERINGS_SIGNALS = SALES_RESPONSE_SCHEMA_OFFERINGS["properties"]["signals"]
_OFFERINGS_SIGNALS["properties"].update(
    {
        "catalog_refs": {"type": "array", "items": {"type": "string"}},
        "cart": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"ref": {"type": "string"}, "qty": {"type": "integer"}},
                "required": ["ref", "qty"],
            },
        },
        "offered_price": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {"ref": {"type": "string"}, "amount": {"type": "integer"}},
            "required": ["ref", "amount"],
        },
    }
)
_OFFERINGS_SIGNALS["required"] = list(_OFFERINGS_SIGNALS["properties"])

_FALLBACK_QUESTION = "What are you hoping to achieve?"


def _extract_question(item: dict | str) -> str:
    """Return the question text from a dict item or plain string."""
    if isinstance(item, dict):
        return item.get("question") or _FALLBACK_QUESTION
    if isinstance(item, str) and item.strip():
        return item.strip()
    return _FALLBACK_QUESTION


def _plans_block(pricing_tiers: list | None, currency: str, annual_discount_pct: int) -> str:
    """Render the tenant's configured pricing tiers into a prompt section so the
    agent can answer plan questions directly instead of deflecting."""
    if not pricing_tiers:
        return ""
    from app.sales.pricing import _currency_symbol

    sym = _currency_symbol(currency)
    lines: list[str] = []
    for t in pricing_tiers:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name", "")).strip()
        if not name:
            continue
        base = int(t.get("base_price", 0) or 0)
        mn = int(t.get("min_users", 0) or 0)
        mx = int(t.get("max_users", 0) or 0)
        over = int(t.get("per_seat_overage_price", 0) or 0)
        lines.append(
            f"- {name}: {sym}{base:,} base for {mn}–{mx} users "
            f"({sym}{over:,} per additional seat)."
        )
    if not lines:
        return ""
    block = "\n## Available Plans\n" + "\n".join(lines)
    if annual_discount_pct:
        block += f"\nAnnual billing gets {annual_discount_pct}% off."
    return block + "\n"


_KNOWN_FACT_LABELS: dict[str, str] = {
    "name": "name",
    "email": "email",
    "company": "company",
    "team_size": "team size",
    "industry": "industry",
    "plan_interest": "plan of interest",
}


def _known_facts_block(known_facts: dict | None) -> str:
    """Pin facts already gathered this conversation so the agent never re-asks."""
    if not known_facts:
        return ""
    lines = [
        f"- {_KNOWN_FACT_LABELS.get(k, k)}: {v}"
        for k, v in known_facts.items()
        if v not in (None, "", []) and k in _KNOWN_FACT_LABELS
    ]
    if not lines:
        return ""
    return (
        "\n## Known facts (already provided this conversation — do NOT ask for "
        "these again; use them directly)\n" + "\n".join(lines) + "\n"
    )


def _meeting_rule(meeting_booking_enabled: bool) -> str:
    """Rule 6 — the model must KNOW whether it can actually schedule, or it
    deflects ('contact support to schedule') even when booking is enabled."""
    if meeting_booking_enabled:
        return (
            "6. Meeting scheduling IS available — you can book it right here. When the "
            "prospect asks to book a meeting, demo, or call, set signals.offer_meeting=true "
            "and tell them you'll show available times to pick from. Do NOT tell them to "
            "contact support or wait for a human to schedule — you schedule it directly. "
            "Do NOT set offer_meeting for mere pricing questions or contact collection.\n"
        )
    return (
        "6. Meeting scheduling is NOT available and you cannot schedule a meeting. If the "
        "prospect asks to book one, offer to connect them with your team instead, and do "
        "NOT set signals.offer_meeting.\n"
    )


def build_sales_directive(
    qualification_questions: list,
    pricing_tiers: list | None = None,
    currency: str = "NGN",
    annual_discount_pct: int = 0,
    known_facts: dict | None = None,
    meeting_booking_enabled: bool = False,
    company_website: str | None = None,
) -> str:
    """Return a system-prompt addendum for sales-intent conversations.

    Injects the tenant's configured plans, company website, and any facts already
    captured this conversation, tells the model whether it can actually schedule
    meetings, and instructs it on qualifying, contact capture, quote signals,
    one-question pacing, higher-tier recommendation, and single-offer escalation.
    """
    if qualification_questions:
        first_question = _extract_question(qualification_questions[0])
    else:
        first_question = _FALLBACK_QUESTION

    website_block = ""
    if company_website:
        website_block = (
            f"\n## Company website\n{company_website} — share this exact URL when a "
            "prospect asks where to sign up, register, or learn more.\n"
        )

    return (
        "## Sales Engagement Directive\n\n"
        "When the conversation involves a sales intent (e.g. pricing, demo, purchase):\n\n"
        "1. Answer the prospect's question clearly and helpfully, using the plan "
        "details below when the question is about plans or pricing.\n"
        "2. Ask AT MOST ONE question per reply. Never combine a qualifying question "
        "with an offer to connect them with a human in the same message. Your "
        f'qualifying question is: "{first_question}"\n'
        "3. Once the prospect has answered, ask for their name and email "
        "conversationally — no forms — just reply here.\n"
        "4. Never sell or upsell when the intent is SUPPORT; focus solely on "
        "resolving the support issue.\n"
        "5. Populate `captured` and `signals` only with facts the prospect has "
        "explicitly stated — do not infer or hallucinate missing values.\n"
        + _meeting_rule(meeting_booking_enabled)
        + "7. For pricing/purchase intent: answer plan questions directly from the plan "
        "details below, gather the prospect's team size and which plan they want, then "
        "set signals.offer_quote=true once you can quote. Put the plan name in "
        "captured.plan_interest.\n"
        "8. When a team size fits more than one plan, recommend the HIGHER plan and "
        "briefly say why (more headroom / room to grow). Be helpful, never pushy — "
        "if the prospect prefers the smaller plan, respect that.\n"
        "9. Never ask for something the prospect has already provided (see Known "
        "facts, if present). Acknowledge what you know and move the conversation "
        "forward rather than repeating a question.\n"
        "10. Offer to connect the prospect with your team ONCE, and only when you "
        "genuinely cannot help further — never as a routine sign-off on every reply.\n"
        "11. Never output placeholder text such as '[insert URL here]' or '[link]'. If "
        "you do not have a specific detail, say so plainly or use the company website "
        "below — never invent a bracketed placeholder.\n"
        "12. You cannot send email, files or documents yourself. Never offer, promise or ask "
        "to email, send or forward a quote or anything else, and never say you will: the "
        "platform sends a quote by email on its own when it can and tells the prospect itself. "
        "You may still ask for the prospect's email address as contact details for follow-up.\n"
        + website_block
        + _plans_block(pricing_tiers, currency, annual_discount_pct)
        + _known_facts_block(known_facts)
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
