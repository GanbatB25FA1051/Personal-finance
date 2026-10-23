"""Shared statement models and exact money conversion."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass
class ParsedRow:
    txn_datetime: str
    amount: int
    balance_after: int
    raw_description: str
    counterparty_account: str | None
    branch_code: str | None


@dataclass
class ParsedStatement:
    bank_code: str
    account_number: str
    currency: str
    period_start: str | None
    period_end: str | None
    rows: list[ParsedRow]
    control_credit: int | None
    control_debit: int | None
    warnings: list[str]


@dataclass
class ImportResult:
    import_id: int
    rows_parsed: int
    rows_new: int
    rows_duplicate: int
    auto_classified: int
    needs_review: int
    warnings: list[str]


def to_minor(value: Decimal | str | float) -> int:
    """Convert major units to minor units with half-up rounding."""
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
