from typing import TypedDict

SIGNALS: list[tuple[str, str, int]] = [
    ("provided_email", "Provided email", 20),
    ("company_name", "Mentioned company name", 10),
    ("team_over_50", "Team size > 50", 15),
    ("enterprise", "Enterprise / corporate", 20),
    ("asked_features", "Asked about specific features", 10),
    ("requested_demo", "Requested a demo", 15),
    ("mentioned_competitor", "Mentioned a competitor", 10),
    ("ready_to_buy", 'Said "ready to buy"', 25),
    ("paid_ad", "Came from a paid ad (UTM)", 5),
]

TIER_THRESHOLDS = {"warm": 31, "hot": 61, "sales_ready": 86}
_ENTERPRISE_HINTS = ("bank", "enterprise", "corporate", "corp", "plc", "group")
_PAID_HINTS = ("cpc", "ppc", "paid", "ad", "ads", "adwords", "google_ads")


class ScoreResult(TypedDict):
    score: int
    tier: str
    breakdown: list[dict]


def tier_for(score: int) -> str:
    if score >= TIER_THRESHOLDS["sales_ready"]:
        return "sales_ready"
    if score >= TIER_THRESHOLDS["hot"]:
        return "hot"
    if score >= TIER_THRESHOLDS["warm"]:
        return "warm"
    return "cold"


def _enterprise(lead) -> bool:
    blob = " ".join(
        filter(None, [getattr(lead, "industry", None), getattr(lead, "company", None)])
    ).lower()
    return any(h in blob for h in _ENTERPRISE_HINTS)


def _paid_ad(lead) -> bool:
    utm = getattr(lead, "utm", None) or {}
    vals = " ".join(str(v).lower() for v in utm.values())
    return any(h in vals for h in _PAID_HINTS)


def score_lead(lead, signals: dict | None = None) -> ScoreResult:
    signals = signals or {}
    field_fired = {
        "provided_email": bool(getattr(lead, "email", None)),
        "company_name": bool(getattr(lead, "company", None)),
        "team_over_50": (getattr(lead, "team_size", None) or 0) > 50,
        "enterprise": _enterprise(lead),
        "paid_ad": _paid_ad(lead),
    }
    breakdown, total = [], 0
    for key, label, weight in SIGNALS:
        fired = field_fired.get(key, bool(signals.get(key, False)))
        if fired:
            total += weight
        breakdown.append({"key": key, "label": label, "weight": weight, "fired": fired})
    score = min(100, total)
    return {"score": score, "tier": tier_for(score), "breakdown": breakdown}
