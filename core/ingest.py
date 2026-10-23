"""Atomic statement import, duplicate detection and fee linking."""
import hashlib
import io
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from openpyxl import load_workbook
from banks.base import detect_parser
from core.models import ImportResult


def import_statement(conn: sqlite3.Connection, file: str | Path | bytes | BinaryIO,
                     filename: str) -> ImportResult:
    """Import one workbook atomically; duplicate rows remain untouched."""
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
        raise ValueError('Unrecognized file/bank format: please select a valid .xlsx statement') from exc
    try:
        parser = detect_parser(wb)
        if parser is None:
            raise ValueError('Unrecognized file/bank format')
        statement = parser.parse(wb)
    finally:
        wb.close()
    if not statement.account_number or not statement.rows:
        raise ValueError('Statement has no account number or readable transaction rows')
    digest = hashlib.sha256(data).hexdigest()
    warnings = list(statement.warnings)
    if conn.execute('SELECT 1 FROM imports WHERE file_hash=?', (digest,)).fetchone():
        warnings.append('This exact file was already imported')
    conn.execute('SAVEPOINT statement_import')
    try:
        conn.execute('INSERT INTO accounts(bank_code,account_number,currency) VALUES (?,?,?) ON CONFLICT(account_number) DO NOTHING',
                     (statement.bank_code, statement.account_number, statement.currency))
        account_id = conn.execute('SELECT id FROM accounts WHERE account_number=?', (statement.account_number,)).fetchone()[0]
        existing = conn.execute('SELECT id,txn_datetime,balance_after FROM transactions WHERE account_id=? ORDER BY txn_datetime,id',
                                (account_id,)).fetchall()
        cursor = conn.execute('''INSERT INTO imports(account_id,filename,file_hash,period_start,period_end,imported_at)
            VALUES (?,?,?,?,?,?)''', (account_id, filename, digest, statement.period_start, statement.period_end,
                                     datetime.now().isoformat(timespec='seconds')))
        import_id = cursor.lastrowid
        new_ids, new_rows = [], []
        fee_category = conn.execute("SELECT id FROM categories WHERE name='Bank Fees & Charges'").fetchone()[0]
        for row in statement.rows:
            key = hashlib.sha256(f'{account_id}|{row.txn_datetime}|{row.amount}|{row.balance_after}|{row.raw_description}'.encode('utf-8')).hexdigest()
            fee = parser.is_fee(row.raw_description)
            parent = None
            if fee:
                previous = conn.execute('''SELECT id FROM transactions WHERE account_id=? AND txn_datetime=?
                    AND classified_by!='system' ORDER BY id DESC LIMIT 1''', (account_id, row.txn_datetime)).fetchone()
                parent = previous[0] if previous else None
            cursor = conn.execute('''INSERT INTO transactions(account_id,import_id,txn_datetime,amount,direction,
                balance_after,raw_description,counterparty_account,branch_code,merchant_clean,category_id,
                classified_by,parent_txn_id,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO NOTHING''',
                (account_id, import_id, row.txn_datetime, row.amount, 'in' if row.amount > 0 else 'out',
                 row.balance_after, row.raw_description, row.counterparty_account, row.branch_code,
                 '' if fee else parser.clean_merchant(row.raw_description), fee_category if fee else None,
                 'system' if fee else 'none', parent, key))
            if cursor.rowcount:
                new_ids.append(cursor.lastrowid)
                new_rows.append(row)
        if new_rows:
            first = min(new_rows, key=lambda r: r.txn_datetime)
            prior = [r for r in existing if r['txn_datetime'] < first.txn_datetime]
            if prior and abs(prior[-1]['balance_after'] - (first.balance_after - first.amount)) > 1:
                warnings.append('Possible gap in statement history')
        try:
            from core.classify import (
                apply_rules,
                classify_remaining,
                learn_rules_from_manual,
            )
        except ImportError:
            auto = 0
        else:
            learn_rules_from_manual(conn)
            auto = apply_rules(conn, new_ids)
            auto += classify_remaining(conn, new_ids)
        needs = conn.execute('SELECT COUNT(*) FROM transactions WHERE import_id=? AND category_id IS NULL', (import_id,)).fetchone()[0]
        total, added = len(statement.rows), len(new_ids)
        conn.execute('UPDATE imports SET rows_parsed=?,rows_new=?,rows_duplicate=? WHERE id=?',
                     (total, added, total - added, import_id))
        conn.execute('RELEASE SAVEPOINT statement_import')
        conn.commit()
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT statement_import')
        conn.execute('RELEASE SAVEPOINT statement_import')
        conn.commit()
        raise
    return ImportResult(import_id, total, added, total - added, auto, needs, warnings)
