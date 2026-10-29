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
    rows_skipped: int = 0
    fatal_errors: list[str] | None = None


@dataclass
class ImportPreview:
    filename: str
    file_data: bytes
    file_hash: str
    statement: ParsedStatement
    parser: object
    rows_new: int
    rows_duplicate: int
    rows_skipped: int
    income_total_minor: int
    expense_total_minor: int
    warnings: list[str]
    fatal_errors: list[str]
    sample_rows: list[dict[str, object]]

    @property
    def masked_account_number(self) -> str:
        """Return the account identifier with only its final four characters."""
        account = self.statement.account_number
        if len(account) <= 4:
            return '*' * len(account)
        return '*' * (len(account) - 4) + account[-4:]

    @property
    def requires_confirmation(self) -> bool:
        """Whether non-fatal parsing warnings need user approval."""
        return bool(self.warnings)


@dataclass
class ImportResult:
    import_id: int
    rows_parsed: int
    rows_new: int
    rows_duplicate: int
    auto_classified: int
    needs_review: int
    warnings: list[str]
    rows_skipped: int = 0


def to_minor(value: Decimal | str | float) -> int:
    """Convert major units to minor units with half-up rounding."""
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
