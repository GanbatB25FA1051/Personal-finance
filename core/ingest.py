"""Statement preview, validation, and atomic persistence."""
import hashlib
import io
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from openpyxl import load_workbook

from banks.base import detect_parser
from core.models import ImportPreview, ImportResult, ParsedRow, ParsedStatement


def _read_statement(
    file: str | Path | bytes | BinaryIO,
) -> tuple[bytes, object, ParsedStatement]:
    """Read and parse an Excel statement, always closing its workbook."""
    try:
        if isinstance(file, (str, Path)):
            data = Path(file).read_bytes()
        elif isinstance(file, bytes):
            data = file
        else:
            file.seek(0)
            data = file.read()
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(
            'Unrecognized file/bank format: please select a valid .xlsx statement'
        ) from exc
    try:
        parser = detect_parser(wb)
        if parser is None:
            raise ValueError('Unrecognized file/bank format')
        statement = parser.parse(wb)
    finally:
        wb.close()
    fatal_errors = statement.fatal_errors or []
    if fatal_errors:
        raise ValueError('; '.join(fatal_errors))
    if not statement.account_number or not statement.rows:
        raise ValueError(
            'Statement has no account number or readable transaction rows'
        )
    return data, parser, statement


def statement_account_number(data: bytes) -> str:
    """Read the account number from a supported workbook."""
    _, _, statement = _read_statement(data)
    return statement.account_number


def _dedupe_key(account_id: int, row: ParsedRow) -> str:
    """Build the stable fingerprint used for existing and in-file duplicates."""
    source = (
        f'{account_id}|{row.txn_datetime}|{row.amount}|'
        f'{row.balance_after}|{row.raw_description}'
    )
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


def _count_new_and_duplicates(
    conn: sqlite3.Connection,
    account_id: int | None,
    rows: list[ParsedRow],
) -> tuple[int, int, list[ParsedRow]]:
    """Count duplicates and return rows that are new to the account."""
    existing_keys: set[str] = set()
    if account_id is not None:
        existing_keys = {
            _dedupe_key(
                account_id,
                ParsedRow(
                    row['txn_datetime'], row['amount'], row['balance_after'],
                    row['raw_description'], None, None,
                ),
            )
            for row in conn.execute(
                '''SELECT txn_datetime, amount, balance_after, raw_description
                   FROM transactions WHERE account_id=?''',
                (account_id,),
            ).fetchall()
        }
    fingerprint_account = account_id if account_id is not None else 0
    seen: set[str] = set()
    new_count = duplicate_count = 0
    new_rows: list[ParsedRow] = []
    for row in rows:
        key = _dedupe_key(fingerprint_account, row)
        if key in existing_keys or key in seen:
            duplicate_count += 1
        else:
            new_count += 1
            new_rows.append(row)
        seen.add(key)
    return new_count, duplicate_count, new_rows


def preview_statement(
    conn: sqlite3.Connection,
    file: str | Path | bytes | BinaryIO,
    filename: str,
) -> ImportPreview:
    """Parse and validate a statement without making database writes."""
    data, parser, statement = _read_statement(file)
    account = conn.execute(
        'SELECT id FROM accounts WHERE account_number=?',
        (statement.account_number,),
    ).fetchone()
    account_id = int(account['id']) if account else None
    rows_new, rows_duplicate, new_rows = _count_new_and_duplicates(
        conn, account_id, statement.rows
    )
    warnings = list(statement.warnings)
    if account_id is not None and new_rows:
        first_new = min(new_rows, key=lambda row: row.txn_datetime)
        prior = conn.execute(
            '''SELECT balance_after FROM transactions
               WHERE account_id=? AND txn_datetime<?
               ORDER BY txn_datetime DESC, id DESC LIMIT 1''',
            (account_id, first_new.txn_datetime),
        ).fetchone()
        if prior and abs(
            prior['balance_after'] - (first_new.balance_after - first_new.amount)
        ) > 1:
            warnings.append('Possible gap in statement history')
    file_hash = hashlib.sha256(data).hexdigest()
    if conn.execute(
        'SELECT 1 FROM imports WHERE file_hash=?', (file_hash,)
    ).fetchone():
        warning = 'This exact file was already imported'
        if warning not in warnings:
            warnings.append(warning)
    samples = [
        {
            'date': row.txn_datetime,
            'amount_minor': row.amount,
            'description': row.raw_description[:160],
        }
        for row in statement.rows[:5]
    ]
    return ImportPreview(
        filename=filename,
        file_data=data,
        file_hash=file_hash,
        statement=statement,
        parser=parser,
        rows_new=rows_new,
        rows_duplicate=rows_duplicate,
        rows_skipped=statement.rows_skipped,
        income_total_minor=sum(max(0, row.amount) for row in statement.rows),
        expense_total_minor=sum(max(0, -row.amount) for row in statement.rows),
        warnings=warnings,
        fatal_errors=list(statement.fatal_errors or []),
        sample_rows=samples,
    )


def import_preview(
    conn: sqlite3.Connection,
    preview: ImportPreview,
    *,
    accept_warnings: bool = False,
) -> ImportResult:
    """Persist the reviewed preview after explicit approval of its warnings."""
    if preview.fatal_errors:
        raise ValueError('; '.join(preview.fatal_errors))
    if preview.requires_confirmation and not accept_warnings:
        raise ValueError('Import warnings require explicit confirmation')
    return _persist_preview(conn, preview)


def import_statement(
    conn: sqlite3.Connection,
    file: str | Path | bytes | BinaryIO,
    filename: str,
    *,
    accept_warnings: bool = True,
) -> ImportResult:
    """Compatibility API for trusted callers; UI should use preview first."""
    preview = preview_statement(conn, file, filename)
    return import_preview(conn, preview, accept_warnings=accept_warnings)


def _persist_preview(
    conn: sqlite3.Connection,
    preview: ImportPreview,
) -> ImportResult:
    """Atomically persist the exact bytes and rows represented by a preview."""
    data = preview.file_data
    digest = preview.file_hash
    filename = preview.filename
    statement = preview.statement
    parser = preview.parser
    warnings = list(preview.warnings)
    caller_transaction = bool(getattr(conn, 'in_transaction', False))
    if not caller_transaction:
        conn.execute('BEGIN')
    conn.execute('SAVEPOINT statement_import')
    savepoint_active = True
    try:
        conn.execute(
            '''INSERT INTO accounts(bank_code, account_number, currency)
               VALUES (?, ?, ?) ON CONFLICT(account_number) DO NOTHING''',
            (statement.bank_code, statement.account_number, statement.currency),
        )
        account = conn.execute(
            'SELECT id FROM accounts WHERE account_number=?',
            (statement.account_number,),
        ).fetchone()
        if account is None:
            raise RuntimeError('Imported account could not be retrieved.')
        account_id = int(account['id'])
        current_counts = _count_new_and_duplicates(
            conn, account_id, statement.rows
        )[:2]
        if current_counts != (preview.rows_new, preview.rows_duplicate):
            raise ValueError(
                'Statement data changed after preview; create a new preview before importing.'
            )
        existing = conn.execute(
            '''SELECT id, txn_datetime, balance_after FROM transactions
               WHERE account_id=? ORDER BY txn_datetime, id''',
            (account_id,),
        ).fetchall()
        cursor = conn.execute(
            '''INSERT INTO imports(
                   account_id, filename, file_hash, period_start, period_end,
                   imported_at
               ) VALUES (?, ?, ?, ?, ?, ?)''',
            (
                account_id, filename, digest, statement.period_start,
                statement.period_end, datetime.now().isoformat(timespec='seconds'),
            ),
        )
        import_id = int(cursor.lastrowid)
        conn.execute(
            '''INSERT INTO import_files(import_id, file_data, file_hash)
               VALUES (?, ?, ?) RETURNING import_id''',
            (import_id, data, digest),
        )
        new_ids: list[int] = []
        new_rows: list[ParsedRow] = []
        fee_category = conn.execute(
            "SELECT id FROM categories WHERE name='Bank Fees & Charges'"
        ).fetchone()[0]
        for row in statement.rows:
            key = _dedupe_key(account_id, row)
            fee = parser.is_fee(row.raw_description)
            parent = None
            if fee:
                previous = conn.execute(
                    '''SELECT id FROM transactions
                       WHERE account_id=? AND txn_datetime=?
                         AND classified_by!='system'
                       ORDER BY id DESC LIMIT 1''',
                    (account_id, row.txn_datetime),
                ).fetchone()
                parent = previous[0] if previous else None
            cursor = conn.execute(
                '''INSERT INTO transactions(
                       account_id, import_id, txn_datetime, amount, direction,
                       balance_after, raw_description, counterparty_account,
                       branch_code, merchant_clean, category_id, classified_by,
                       parent_txn_id, dedupe_key
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(dedupe_key) DO NOTHING''',
                (
                    account_id, import_id, row.txn_datetime, row.amount,
                    'in' if row.amount > 0 else 'out', row.balance_after,
                    row.raw_description, row.counterparty_account, row.branch_code,
                    '' if fee else parser.clean_merchant(row.raw_description),
                    fee_category if fee else None,
                    'system' if fee else 'none', parent, key,
                ),
            )
            transaction = conn.execute(
                'SELECT id FROM transactions WHERE dedupe_key=?', (key,)
            ).fetchone()
            if transaction is None:
                raise RuntimeError('Imported transaction could not be retrieved.')
            conn.execute(
                '''INSERT INTO import_transactions(import_id, transaction_id)
                   VALUES (?, ?) ON CONFLICT(import_id, transaction_id) DO NOTHING
                   RETURNING import_id''',
                (import_id, transaction['id']),
            )
            if cursor.rowcount:
                new_ids.append(int(cursor.lastrowid))
                new_rows.append(row)
        try:
            from core.classify import apply_rules, classify_remaining
        except ImportError:
            auto = 0
        else:
            auto = apply_rules(conn, new_ids)
            auto += classify_remaining(conn, new_ids)
        needs = conn.execute(
            '''SELECT COUNT(DISTINCT t.id)
               FROM transactions AS t
               JOIN import_transactions AS it ON it.transaction_id=t.id
               WHERE it.import_id=? AND t.category_id IS NULL''',
            (import_id,),
        ).fetchone()[0]
        total, added = len(statement.rows), len(new_ids)
        conn.execute(
            '''UPDATE imports
               SET rows_parsed=?, rows_new=?, rows_duplicate=? WHERE id=?''',
            (total, added, total - added, import_id),
        )
        conn.execute('RELEASE SAVEPOINT statement_import')
        savepoint_active = False
        if not caller_transaction:
            conn.commit()
    except Exception:
        if savepoint_active:
            try:
                conn.execute('ROLLBACK TO SAVEPOINT statement_import')
                conn.execute('RELEASE SAVEPOINT statement_import')
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
        else:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    return ImportResult(
        import_id, total, added, total - added, auto, needs, warnings,
        preview.rows_skipped,
    )
