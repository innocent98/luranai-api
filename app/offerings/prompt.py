"""Channel formatting and the catalog selling rules added to the sales directive."""

_FORMATTING = {
    "whatsapp": (
        "Format for WhatsApp: plain text, no markdown (no asterisks, headings or tables), short "
        "lines, one item per line starting with its #ref."
    ),
    "voice": (
        "The reply is read aloud: short sentences, no lists, no symbols other than the currency "
        "sign, and no #refs."
    ),
}


def channel_formatting(channel: str | None) -> str | None:
    return _FORMATTING.get(channel or "")


def catalog_sales_addendum(channel: str | None) -> str:
    """Rules for the structured fields; appended to the sales system message when grounded."""
    contact = (
        "Do not ask for an email: the customer is on WhatsApp and the team replies in this chat. "
        "Ask for their name only."
        if channel == "whatsapp"
        else "Then ask for their name and email."
    )
    return (
        "## Catalog selling (this turn)\n"
        "- signals.catalog_refs: the #refs of the catalog items your reply is about, only refs "
        "that appear in the catalog block; [] if none.\n"
        "- signals.cart: the complete list of items the customer has explicitly chosen to get so "
        "far in this conversation (repeat earlier choices every turn), each with its #ref and "
        "quantity (at least 1). Never add an item the customer only asked about. [] if none.\n"
        "- signals.offered_price: when the customer states a price they would pay, "
        '{"ref": "#12", "amount": <their figure as a whole number>}; otherwise null. Never '
        "accept or counter it: say the owner is open to offers (if the item is negotiable) and "
        "that the team will follow up.\n"
        "- Ask which item or quantity if it is ambiguous, confirm the chosen items back, "
        f"then ask for their contact. {contact}\n"
        "- Never state a total yourself; the system prepares any quote from the cart."
    )
