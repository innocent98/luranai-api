"""Bulk import of offerings from pasted text or a CSV: parse, preview (no writes) and apply.

Decisions (the spec left them to the builder):

* `mode=add` only creates; a row whose `external_ref` already exists is an error
  (`external_ref_taken`), never a silent update.
* `mode=upsert` matches by `external_ref` first (any status; an archived match is updated and
  reactivated, and counts toward the plan cap), and only a row *without* a ref falls back to a
  case-insensitive name match among ACTIVE items (two active items with that name make the row
  `ambiguous_name`). Another tenant's rows are never matched.
* On an update a blank cell or an absent column leaves the stored value alone (pasting a partial
  list must not wipe descriptions); only fields the row actually provides change.
* The whole import is one transaction: any bad row refuses everything (`import_has_errors`)
  unless `skip_errors` is set. The plan cap is checked on the creates plus reactivations before
  anything is written (`offering_limit`, with how many fit).
* Row numbers are 1-based positions among the non-blank data rows (the header and blank lines do
  not count). A name may not start with `= + - @` (spreadsheet formula injection).
* Text is cleaned (`clean_text`: invisible, bidi and control characters out) BEFORE a row's ref,
  name and formula start are compared, so what is compared is what is stored.
* Parsing must stay linear in the input: the `lines` format collapses whitespace first and refuses
  a line over `MAX_LINE_CHARS`, so no regex ever sees a long run of separators.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.offering import (
    Offering,
    OfferingAvailability,
    OfferingKind,
    OfferingPriceMode,
    OfferingStatus,
)
from app.services.offering_service import (
    OfferingError,
    OfferingService,
    clean_text,
    validate_candidate,
)

MAX_ROWS = 500
MAX_BYTES = 256 * 1024
MAX_LINE_CHARS = 1_000  # a pasted list line: a name, a price, a status; longer is not a list
MAX_AMOUNT_CHARS = 30

# What the preview shows for each row, in this order.
PARSED_FIELDS = (
    "kind",
    "name",
    "price_mode",
    "price",
    "availability",
    "category",
    "description",
    "keywords",
    "price_note",
    "quantity",
    "negotiable",
    "external_ref",
)

_HEADER_ALIASES = {
    "item": "name",
    "product": "name",
    "title": "name",
    "type": "kind",
    "amount": "price",
    "cost": "price",
    "stock": "availability",
    "details": "description",
    "ref": "external_ref",
    "sku": "external_ref",
    "tags": "keywords",
}
_COLUMNS = {
    "name",
    "kind",
    "price",
    "price_mode",
    "price_note",
    "availability",
    "category",
    "description",
    "keywords",
    "quantity",
    "negotiable",
    "external_ref",
}
_KINDS = {
    "product": "product",
    "products": "product",
    "goods": "product",
    "service": "service",
    "services": "service",
}
_AVAILABILITY = {
    "in_stock": "in_stock",
    "instock": "in_stock",
    "available": "in_stock",
    "yes": "in_stock",
    "limited": "limited",
    "low": "limited",
    "few_left": "limited",
    "out_of_stock": "out_of_stock",
    "sold_out": "out_of_stock",
    "soldout": "out_of_stock",
    "unavailable": "out_of_stock",
    "no": "out_of_stock",
    "on_request": "on_request",
    "by_request": "on_request",
    "request": "on_request",
}
_PRICE_MODES = {
    "fixed": "fixed",
    "from": "from",
    "on_request": "on_request",
    "request": "on_request",
}
_ON_REQUEST_WORDS = {
    "on request",
    "on_request",
    "ask",
    "call",
    "contact",
    "tbd",
    "price on request",
}
_TRUE = {"yes", "y", "true", "1", "negotiable", "open"}
_FALSE = {"no", "n", "false", "0"}

_NUMBER = re.compile(r"^(-?)((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)([km]?)$")


class ImportUnreadable(Exception):
    """The content cannot be turned into rows at all."""

    code = "import_unreadable"


class ImportLineTooLong(ImportUnreadable):
    code = "line_too_long"


def parse_amount(text: str) -> int:
    """Whole naira from "12,000", "₦12000", "N2,500", "NGN 3,000", "12k", "1.5m", "12,000 naira".

    Raises ValueError for anything else, including a fractional result (money is whole units).
    """
    if len(text) > MAX_AMOUNT_CHARS:
        raise ValueError("not an amount: too long")
    cleaned = re.sub(r"₦|\bngn\b|naira", "", text.strip().lower()).strip()
    cleaned = re.sub(r"^n\s*(?=[\d.])", "", cleaned)
    match = _NUMBER.match(cleaned)
    if not match:
        raise ValueError(f"not an amount: {text!r}")
    sign, digits, suffix = match.groups()
    try:
        value = Decimal(digits.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - the regex already guarantees digits
        raise ValueError(f"not an amount: {text!r}") from exc
    value *= {"": 1, "k": 1_000, "m": 1_000_000}[suffix]
    if value != value.to_integral_value():
        raise ValueError(f"not a whole amount: {text!r}")
    return int(value) * (-1 if sign else 1)


# --- parsing --------------------------------------------------------------------------------


def _canonical_header(cell: str) -> str:
    key = re.sub(r"[\s\-]+", "_", cell.strip().lower())
    return _HEADER_ALIASES.get(key, key)


def parse_csv(content: str) -> list[tuple[int, dict[str, str]]]:
    """Rows of a CSV with a header line (comma, tab or semicolon separated)."""
    text = content.lstrip("\ufeff")
    if not text.strip() or "\x00" in text:
        raise ImportUnreadable("empty or binary content")
    first_line = next(line for line in text.splitlines() if line.strip())
    delimiter = max((",", "\t", ";"), key=first_line.count)
    try:
        table = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error as exc:
        raise ImportUnreadable("not a readable CSV") from exc
    table = [row for row in table if any(cell.strip() for cell in row)]
    if not table:
        raise ImportUnreadable("no rows")
    columns: dict[int, str] = {}
    for index, cell in enumerate(table[0]):
        name = _canonical_header(cell)
        if name in _COLUMNS and name not in columns.values():
            columns[index] = name
    if "name" not in columns.values():
        raise ImportUnreadable("the header needs a 'name' column")
    rows: list[tuple[int, dict[str, str]]] = []
    for number, cells in enumerate(table[1:], start=1):
        values = {
            columns[i]: cells[i].strip() for i in columns if i < len(cells) and cells[i].strip()
        }
        rows.append((number, values))
    if not rows:
        raise ImportUnreadable("no data rows")
    return rows


_WHITESPACE = re.compile(r"\s+")
_LINE_SPLIT = re.compile(r"\s+[-–—]\s+|\s*\|\s*")


def parse_lines(content: str) -> list[tuple[int, dict[str, str]]]:
    """`Name - 12000 - in stock` per line (the list a seller pastes from WhatsApp)."""
    if "\x00" in content:
        raise ImportUnreadable("binary content")
    rows: list[tuple[int, dict[str, str]]] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        if len(line) > MAX_LINE_CHARS:
            raise ImportLineTooLong(
                f"line {len(rows) + 1} is longer than {MAX_LINE_CHARS:,} characters"
            )
        # Collapse separators to one space first: `_LINE_SPLIT` backtracks quadratically on a long
        # run of whitespace, and a name with doubled spaces reads better collapsed anyway.
        parts = [p.strip() for p in _LINE_SPLIT.split(_WHITESPACE.sub(" ", line.strip()))]
        values = {"name": parts[0]}
        if len(parts) > 1 and parts[1]:
            values["price"] = parts[1]
        if len(parts) > 2 and parts[2]:
            values["availability"] = parts[2]
        rows.append((len(rows) + 1, values))
    if not rows:
        raise ImportUnreadable("no lines")
    return rows


# --- one row --------------------------------------------------------------------------------


def _key(value: str) -> str:
    return re.sub(r"[\s\-]+", "_", value.strip().lower())


def normalize_row(raw: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Typed fields the row provides (nothing for blank cells) and its conversion errors."""
    fields: dict[str, Any] = {}
    errors: list[dict[str, str]] = []

    def fail(name: str, code: str) -> None:
        errors.append({"field": name, "code": code})

    # Clean first: the ref and the name are compared (duplicates, upsert matches) and checked
    # (formula start) as they will be stored. A cell that is blank after cleaning is a blank cell.
    for name in ("name", "description", "category", "keywords", "price_note", "external_ref"):
        cleaned = clean_text(raw.get(name), multiline=name == "description")
        if cleaned:
            fields[name] = cleaned

    if raw.get("kind"):
        kind = _KINDS.get(_key(raw["kind"]))
        fields["kind"] = kind
        if kind is None:
            fail("kind", "kind_invalid")
    if raw.get("availability"):
        availability = _AVAILABILITY.get(_key(raw["availability"]))
        fields["availability"] = availability
        if availability is None:
            fail("availability", "availability_invalid")
    if raw.get("price_mode"):
        mode = _PRICE_MODES.get(_key(raw["price_mode"]))
        fields["price_mode"] = mode
        if mode is None:
            fail("price_mode", "price_mode_invalid")

    if raw.get("price"):
        text = raw["price"].strip()
        if text.lower() in _ON_REQUEST_WORDS:
            fields.setdefault("price_mode", "on_request")
        else:
            if text.lower().startswith("from "):
                text = text[5:]
                fields.setdefault("price_mode", "from")
            try:
                fields["price"] = parse_amount(text)
            except ValueError:
                fail("price", "price_invalid")

    if raw.get("quantity"):
        try:
            fields["quantity"] = int(raw["quantity"].strip())
        except ValueError:
            fail("quantity", "quantity_invalid")
    if raw.get("negotiable"):
        word = raw["negotiable"].strip().lower()
        if word in _TRUE:
            fields["negotiable"] = True
        elif word in _FALSE:
            fields["negotiable"] = False
        else:
            fail("negotiable", "negotiable_invalid")
    return fields, errors


# --- planning -------------------------------------------------------------------------------


@dataclass
class PlannedRow:
    row: int
    status: str  # create | update | error
    parsed: dict[str, Any] | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    target: Offering | None = None
    values: dict[str, Any] = field(default_factory=dict)

    def preview(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "row": self.row,
            "status": self.status,
            "parsed": self.parsed,
            "errors": self.errors,
        }
        if self.target is not None:
            out["target"] = f"#{self.target.seq}"
        return out


@dataclass
class ImportPlan:
    rows: list[PlannedRow]
    new_active: int  # creates + reactivations: what counts toward the plan cap

    def summary(self, over_limit_by: int = 0) -> dict[str, int]:
        count = lambda status: sum(1 for r in self.rows if r.status == status)  # noqa: E731
        return {
            "create": count("create"),
            "update": count("update"),
            "error": count("error"),
            "over_limit_by": over_limit_by,
        }

    @property
    def errored(self) -> list[PlannedRow]:
        return [r for r in self.rows if r.status == "error"]


def _current(item: Offering) -> dict[str, Any]:
    return {
        "kind": item.kind.value,
        "name": item.name,
        "description": item.description,
        "category": item.category,
        "keywords": item.keywords,
        "price_mode": item.price_mode.value,
        "price": item.price,
        "price_note": item.price_note,
        "availability": item.availability.value,
        "quantity": item.quantity,
        "negotiable": item.negotiable,
        "external_ref": item.external_ref,
    }


def _defaults_for_create(fields: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        "kind": "product",
        "availability": "in_stock",
        "negotiable": False,
        "description": None,
        "category": None,
        "keywords": None,
        "price_note": None,
        "quantity": None,
        "external_ref": None,
        "price": None,
        **fields,
    }
    if "price_mode" not in fields:
        candidate["price_mode"] = "fixed" if fields.get("price") is not None else "on_request"
    return candidate


def _merge_for_update(current: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    candidate = {**current, **fields}
    if candidate["price_mode"] == "on_request" and "price" not in fields:
        candidate["price"] = None
    elif "price" in fields and "price_mode" not in fields and current["price_mode"] == "on_request":
        candidate["price_mode"] = "fixed"
    return candidate


def build_plan(
    db: Session, tenant_id: UUID, rows: list[tuple[int, dict[str, str]]], mode: str
) -> ImportPlan:
    existing = list(db.execute(select(Offering).where(Offering.tenant_id == tenant_id)).scalars())
    by_ref = {i.external_ref: i for i in existing if i.external_ref}
    by_name: dict[str, list[Offering]] = {}
    for item in existing:
        if item.status == OfferingStatus.ACTIVE:
            by_name.setdefault(item.name.strip().lower(), []).append(item)

    planned: list[PlannedRow] = []
    seen_refs: set[str] = set()
    seen_targets: set[UUID] = set()
    new_active = 0

    for number, raw in rows:
        fields, errors = normalize_row(raw)
        target: Offering | None = None
        ref = fields.get("external_ref")

        if ref:
            if ref in seen_refs:
                errors.append({"field": "external_ref", "code": "duplicate_in_file"})
            seen_refs.add(ref)
            if mode == "upsert":
                target = by_ref.get(ref)
            elif ref in by_ref:
                errors.append({"field": "external_ref", "code": "external_ref_taken"})
        elif mode == "upsert" and fields.get("name"):
            matches = by_name.get(fields["name"].strip().lower(), [])
            if len(matches) > 1:
                errors.append({"field": "name", "code": "ambiguous_name"})
            elif matches:
                target = matches[0]
        if target is not None:
            if target.id in seen_targets:
                errors.append(
                    {"field": "external_ref" if ref else "name", "code": "duplicate_in_file"}
                )
            seen_targets.add(target.id)

        candidate = (
            _merge_for_update(_current(target), fields)
            if target is not None
            else _defaults_for_create(fields)
        )
        clean, validation = validate_candidate(candidate)
        failed = {e["field"] for e in errors}
        errors += [e for e in validation if e["field"] not in failed]

        if errors:
            planned.append(PlannedRow(number, "error", errors=errors))
            continue
        parsed = {name: clean.get(name) for name in PARSED_FIELDS}
        planned.append(
            PlannedRow(
                number,
                "update" if target is not None else "create",
                parsed=parsed,
                target=target,
                values=clean,
            )
        )
        if target is None or target.status != OfferingStatus.ACTIVE:
            new_active += 1
    return ImportPlan(planned, new_active)


def _row_errors(plan: ImportPlan) -> list[dict[str, Any]]:
    return [{"row": r.row, "errors": r.errors} for r in plan.errored]


class OfferingImportService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.offerings = OfferingService(db)

    def _parse(self, fmt: str, content: str) -> list[tuple[int, dict[str, str]]]:
        if len(content.encode("utf-8")) > MAX_BYTES:
            raise OfferingError(
                422, "import_too_large", "The import is larger than 256 KB. Split it into parts."
            )
        try:
            rows = parse_csv(content) if fmt == "csv" else parse_lines(content)
        except ImportUnreadable as exc:
            raise OfferingError(
                422, exc.code, f"The import could not be read: {exc}."
            ) from exc
        if len(rows) > MAX_ROWS:
            raise OfferingError(
                422,
                "import_too_large",
                f"The import has {len(rows)} rows; the limit is {MAX_ROWS}. Split it into parts.",
            )
        return rows

    def preview(
        self, tenant_id: UUID, entitlements: Any, fmt: str, content: str, mode: str
    ) -> dict:
        rows = self._parse(fmt, content)
        plan = build_plan(self.db, tenant_id, rows, mode)
        limit = entitlements.limit("offerings")
        over = 0
        if limit is not None:
            over = max(0, self.offerings.active_count(tenant_id) + plan.new_active - limit)
        return {"rows": [r.preview() for r in plan.rows], "summary": plan.summary(over)}

    def apply(
        self,
        tenant_id: UUID,
        fmt: str,
        content: str,
        mode: str,
        skip_errors: bool,
    ) -> dict[str, Any]:
        rows = self._parse(fmt, content)
        entitlements = self.offerings.require_write(tenant_id)
        settings = self.offerings.lock_settings(tenant_id)
        plan = build_plan(self.db, tenant_id, rows, mode)

        if plan.errored and not skip_errors:
            self.db.commit()  # nothing written: release the settings lock
            raise OfferingError(
                422,
                "import_has_errors",
                "Some rows are not valid, so nothing was imported. Fix them or skip them.",
                errors=_row_errors(plan),
                summary=plan.summary(),
            )

        limit = entitlements.limit("offerings")
        used = self.offerings.active_count(tenant_id)
        if limit is not None and used + plan.new_active > limit:
            self.db.commit()
            fits = max(0, limit - used)
            raise OfferingError(
                409,
                "offering_limit",
                f"Your plan allows {limit:,} items and you have {used:,}. "
                f"This import would add {plan.new_active:,}; only {fits:,} fit.",
                limit=limit,
                used=used,
                can_add=fits,
            )

        now = datetime.now(UTC)
        created = updated = 0
        for planned in plan.rows:
            if planned.status == "create":
                values = planned.values
                self.db.add(
                    Offering(
                        tenant_id=tenant_id,
                        seq=settings.next_seq,
                        kind=OfferingKind(values["kind"]),
                        name=values["name"],
                        description=values.get("description"),
                        category=values.get("category"),
                        keywords=values.get("keywords"),
                        price_mode=OfferingPriceMode(values["price_mode"]),
                        price=values.get("price"),
                        price_note=values.get("price_note"),
                        availability=OfferingAvailability(values["availability"]),
                        quantity=values.get("quantity"),
                        negotiable=bool(values.get("negotiable")),
                        status=OfferingStatus.ACTIVE,
                        external_ref=values.get("external_ref"),
                        availability_changed_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                settings.next_seq += 1
                created += 1
            elif planned.status == "update" and planned.target is not None:
                item, values = planned.target, planned.values
                if values["availability"] != item.availability.value:
                    item.availability_changed_at = now
                item.kind = OfferingKind(values["kind"])
                item.name = values["name"]
                item.description = values.get("description")
                item.category = values.get("category")
                item.keywords = values.get("keywords")
                item.price_mode = OfferingPriceMode(values["price_mode"])
                item.price = values.get("price")
                item.price_note = values.get("price_note")
                item.availability = OfferingAvailability(values["availability"])
                item.quantity = values.get("quantity")
                item.negotiable = bool(values.get("negotiable"))
                if values.get("external_ref"):
                    item.external_ref = values["external_ref"]
                if item.status != OfferingStatus.ACTIVE:
                    item.status = OfferingStatus.ACTIVE
                    item.archived_at = None
                item.updated_at = now
                updated += 1
        try:
            self.db.flush()
        except IntegrityError:
            # Unreachable while refs are compared after cleaning and the settings lock is held,
            # but a database refusal must never surface as a 500 with a driver message.
            self.db.rollback()
            raise OfferingError(
                409,
                "import_conflict",
                "Some rows clash with items you already have, so nothing was imported. "
                "Preview the import again.",
            ) from None
        self.offerings.record_activity(tenant_id)
        self.db.commit()
        return {
            "created": created,
            "updated": updated,
            "skipped": len(plan.errored),
            "errors": _row_errors(plan),
        }
