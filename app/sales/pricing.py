"""Pure deterministic quote computation (Stage 3 Slice 4). Integer money math only."""


def _match_tier(tiers: list[dict], team_size: int, plan_interest: str | None) -> dict | None:
    if plan_interest:
        want = plan_interest.strip().lower()
        for t in tiers:
            if str(t.get("name", "")).strip().lower() == want:
                return t
    for t in tiers:
        if int(t.get("min_users", 0)) <= team_size <= int(t.get("max_users", 0)):
            return t
    return None


def compute_quote(
    pricing_tiers: list[dict],
    annual_discount_pct: int,
    currency: str,
    *,
    team_size: int,
    plan_interest: str | None,
) -> dict | None:
    tier = _match_tier(pricing_tiers or [], team_size, plan_interest)
    if tier is None:
        return None

    base = int(tier["base_price"])
    max_users = int(tier["max_users"])
    rate = int(tier.get("per_seat_overage_price", 0))
    overage_seats = max(0, team_size - max_users)
    overage = overage_seats * rate
    subtotal = base + overage
    pct = int(annual_discount_pct or 0)
    discount = subtotal * pct // 100
    total = subtotal - discount

    line_items = [{
        "label": tier["name"],
        "detail": f"{int(tier['min_users'])}–{max_users} users",
        "amount": base,
    }]
    if overage_seats > 0:
        line_items.append({
            "label": f"Extra seats (×{overage_seats})",
            "detail": f"₦{rate:,} / seat",
            "amount": overage,
        })
    if pct > 0 and discount > 0:
        line_items.append({
            "label": "Annual discount",
            "detail": f"−{pct}%",
            "amount": -discount,
        })
    return {"currency": currency, "line_items": line_items, "total": total}
