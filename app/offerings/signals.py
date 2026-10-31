"""Normalises the catalog signals a model returns (spec 7.4); everything else is code's job.

The model's output is untrusted: refs must look like `#12`, quantities and amounts must be real
integers, duplicates fold together, and anything malformed is dropped rather than repaired. Whether
a ref is a live, sellable item is checked later against the database, never here.
"""

import re
from dataclasses import dataclass, field

from app.db.models.offering import MAX_PRICE

MAX_REFS = 5
MAX_QTY = 999
_REF = re.compile(r"^\s*#?(\d{1,9})\s*$")


@dataclass(frozen=True)
class CartLine:
    seq: int
    qty: int


@dataclass(frozen=True)
class OfferedPrice:
    seq: int
    amount: int


@dataclass
class CatalogSignals:
    catalog_seqs: list[int] = field(default_factory=list)
    cart: list[CartLine] = field(default_factory=list)
    offered_price: OfferedPrice | None = None


def _seq(ref) -> int | None:
    if isinstance(ref, int) and not isinstance(ref, bool):
        return ref if ref > 0 else None
    if isinstance(ref, str):
        m = _REF.match(ref)
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return None


def _whole(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_catalog_signals(signals: dict | None) -> CatalogSignals:
    out = CatalogSignals()
    if not isinstance(signals, dict):
        return out

    refs = signals.get("catalog_refs")
    if isinstance(refs, list):
        for ref in refs:
            seq = _seq(ref)
            if seq is not None and seq not in out.catalog_seqs and len(out.catalog_seqs) < MAX_REFS:
                out.catalog_seqs.append(seq)

    cart = signals.get("cart")
    if isinstance(cart, list):
        qty_by_seq: dict[int, int] = {}
        for line in cart:
            if not isinstance(line, dict):
                continue
            seq, qty = _seq(line.get("ref")), _whole(line.get("qty"))
            if seq is None or qty is None or qty < 1:
                continue
            qty_by_seq[seq] = min(MAX_QTY, qty_by_seq.get(seq, 0) + qty)
        out.cart = [CartLine(seq, qty) for seq, qty in qty_by_seq.items()]

    offered = signals.get("offered_price")
    if isinstance(offered, dict):
        seq, amount = _seq(offered.get("ref")), _whole(offered.get("amount"))
        if seq is not None and amount is not None and 0 < amount <= MAX_PRICE:
            out.offered_price = OfferedPrice(seq, amount)
    return out
