"""Explicitly confirmed links between the user's own-account transfers."""

import json
import sqlite3
from datetime import datetime
from typing import Any


def _atomic(conn: Any):
    from contextlib import contextmanager

    @contextmanager
    def operation():
        owns = not bool(getattr(conn, 'in_transaction', False))
        if owns:
            conn.execute('BEGIN')
        conn.execute('SAVEPOINT transfer_pair_operation')
        try:
            yield
            conn.execute('RELEASE SAVEPOINT transfer_pair_operation')
            if owns:
                conn.commit()
        except Exception as original_error:
            try:
                conn.execute('ROLLBACK TO SAVEPOINT transfer_pair_operation')
                conn.execute('RELEASE SAVEPOINT transfer_pair_operation')
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            if owns:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise original_error
    return operation()


def _audit(conn: Any, pair_id: int, action: str, old: dict | None, new: dict | None) -> None:
    conn.execute(
        "INSERT INTO transfer_pair_audit"
        "(pair_id, action, old_values, new_values, changed_at)"
        "VALUES (?, ?, ?, ?, ?)",
        (pair_id, action,
         json.dumps(old, ensure_ascii=False, sort_keys=True),
         json.dumps(new, ensure_ascii=False, sort_keys=True),
         datetime.now().isoformat(timespec='seconds')),
    )


def _masked(value: str | None) -> str:
    value = value or ''
    return ('*' * max(0, len(value) - 4) + value[-4:]) if value else 'дансгүй'


def transfer_candidates(
    conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
    account_id: int | None = None, currency: str | None = 'MNT',
) -> list[dict[str, Any]]:
    clauses = [
        "o.direction='out'", "i.direction='in'", 'o.amount < 0',
        'i.amount > 0', '(-o.amount)=i.amount',
        'substr(o.txn_datetime,1,10)=substr(i.txn_datetime,1,10)',
        'o.account_id IS NOT NULL', 'i.account_id IS NOT NULL',
        'o.account_id != i.account_id',
        "COALESCE(NULLIF(oa.currency,''),'MNT')=COALESCE(NULLIF(ia.currency,''),'MNT')",
        "(oc.type IS NULL OR oc.type!='fee')",
        "(ic.type IS NULL OR ic.type!='fee')",
        "NOT EXISTS (SELECT 1 FROM transfer_pairs tp WHERE tp.outgoing_transaction_id=o.id OR tp.incoming_transaction_id=o.id)",
        "NOT EXISTS (SELECT 1 FROM transfer_pairs tp WHERE tp.outgoing_transaction_id=i.id OR tp.incoming_transaction_id=i.id)",
    ]
    params: list[object] = []
    for alias in ('o', 'i'):
        if start:
            clauses.append(f'substr({alias}.txn_datetime,1,10)>=?')
            params.append(start[:10])
        if end:
            clauses.append(f'substr({alias}.txn_datetime,1,10)<=?')
            params.append(end[:10])
    if account_id is not None:
        clauses.append('(o.account_id=? OR i.account_id=?)')
        params.extend([account_id, account_id])
    if currency:
        clauses.append("COALESCE(NULLIF(oa.currency,''),'MNT')=?")
        clauses.append("COALESCE(NULLIF(ia.currency,''),'MNT')=?")
        params.extend([currency.upper(), currency.upper()])
    rows = conn.execute(
        f"""SELECT o.id AS outgoing_id, i.id AS incoming_id,
                   o.txn_datetime AS outgoing_date, i.txn_datetime AS incoming_date,
                   -o.amount AS amount_minor, COALESCE(NULLIF(oa.currency,''),'MNT') AS currency,
                   o.raw_description AS outgoing_description, i.raw_description AS incoming_description,
                   o.counterparty_account AS outgoing_counterparty,
                   i.counterparty_account AS incoming_counterparty,
                   oa.account_number AS outgoing_number, ia.account_number AS incoming_number,
                   COALESCE(oa.nickname,'') AS outgoing_name, COALESCE(ia.nickname,'') AS incoming_name
            FROM transactions o
            JOIN transactions i ON i.amount=-o.amount
            JOIN accounts oa ON oa.id=o.account_id
            JOIN accounts ia ON ia.id=i.account_id
            LEFT JOIN categories oc ON oc.id=o.category_id
            LEFT JOIN categories ic ON ic.id=i.category_id
            WHERE {' AND '.join(clauses)}
            ORDER BY o.txn_datetime, o.id, i.id""", params,
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        out_number = str(row['outgoing_number'] or '')
        in_number = str(row['incoming_number'] or '')
        out_cp = str(row['outgoing_counterparty'] or '').strip()
        in_cp = str(row['incoming_counterparty'] or '').strip()
        if not out_cp or not in_cp:
            counterpart_match = None
        else:
            counterpart_match = (out_cp.casefold() == in_number.casefold()
                                 and in_cp.casefold() == out_number.casefold())
        results.append({
            'outgoing_id': int(row['outgoing_id']),
            'incoming_id': int(row['incoming_id']),
            'outgoing_date': str(row['outgoing_date']),
            'incoming_date': str(row['incoming_date']),
            'amount_minor': int(row['amount_minor']),
            'currency': str(row['currency']),
            'outgoing_account': f"{row['outgoing_name']} · {_masked(out_number)}".strip(' ·'),
            'incoming_account': f"{row['incoming_name']} · {_masked(in_number)}".strip(' ·'),
            'outgoing_description': str(row['outgoing_description'] or ''),
            'incoming_description': str(row['incoming_description'] or ''),
            'counterpart_match': counterpart_match,
        })
    return results


def available_transfer_transactions(
    conn: sqlite3.Connection, start: str | None = None, end: str | None = None,
    currency: str | None = 'MNT',
) -> list[dict[str, Any]]:
    clauses = [
        't.account_id IS NOT NULL', "(c.type IS NULL OR c.type!='fee')",
        "NOT EXISTS (SELECT 1 FROM transfer_pairs tp WHERE tp.outgoing_transaction_id=t.id OR tp.incoming_transaction_id=t.id)",
    ]
    params: list[object] = []
    if start:
        clauses.append('substr(t.txn_datetime,1,10)>=?'); params.append(start[:10])
    if end:
        clauses.append('substr(t.txn_datetime,1,10)<=?'); params.append(end[:10])
    if currency:
        clauses.append("COALESCE(NULLIF(a.currency,''),'MNT')=?"); params.append(currency.upper())
    rows = conn.execute(
        f"""SELECT t.id, t.txn_datetime, t.amount, t.direction, t.raw_description,
                   a.id AS account_id, a.account_number, a.nickname,
                   COALESCE(NULLIF(a.currency,''),'MNT') AS currency
            FROM transactions t JOIN accounts a ON a.id=t.account_id
            LEFT JOIN categories c ON c.id=t.category_id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.txn_datetime DESC, t.id DESC""", params,
    ).fetchall()
    return [{
        'id': int(row['id']), 'date': str(row['txn_datetime']),
        'amount_minor': int(row['amount']), 'direction': str(row['direction']),
        'description': str(row['raw_description'] or ''),
        'account_id': int(row['account_id']),
        'account': f"{row['nickname'] or ''} · {_masked(row['account_number'])}".strip(' ·'),
        'currency': str(row['currency']),
    } for row in rows]


def confirm_transfer_pair(conn: sqlite3.Connection, outgoing_id: int, incoming_id: int) -> int:
    if int(outgoing_id) == int(incoming_id):
        raise ValueError('A transaction cannot be paired with itself')
    with _atomic(conn):
        rows = conn.execute(
            """SELECT t.id, t.account_id, t.amount, t.direction, t.category_id,
                      COALESCE(NULLIF(a.currency,''),'MNT') AS currency, c.type AS category_type
               FROM transactions t JOIN accounts a ON a.id=t.account_id
               LEFT JOIN categories c ON c.id=t.category_id
               WHERE t.id IN (?, ?)""", (outgoing_id, incoming_id),
        ).fetchall()
        by_id = {int(row['id']): row for row in rows}
        if len(by_id) != 2:
            raise ValueError('Both transfer transactions must exist and have an account')
        outgoing, incoming = by_id[int(outgoing_id)], by_id[int(incoming_id)]
        if outgoing['direction'] != 'out' or incoming['direction'] != 'in':
            raise ValueError('Choose one outgoing and one incoming transaction')
        if outgoing['account_id'] == incoming['account_id']:
            raise ValueError('Transfers must connect two different own accounts')
        if abs(int(outgoing['amount'])) != abs(int(incoming['amount'])):
            raise ValueError('Transfer amounts must match exactly')
        if str(outgoing['currency']).upper() != str(incoming['currency']).upper():
            raise ValueError('Transfer currencies must match')
        if outgoing['category_type'] == 'fee' or incoming['category_type'] == 'fee':
            raise ValueError('Bank fees remain expenses and cannot be paired as transfers')
        if conn.execute(
            'SELECT 1 FROM transfer_pairs WHERE outgoing_transaction_id IN (?,?) OR incoming_transaction_id IN (?,?)',
            (outgoing_id, incoming_id, outgoing_id, incoming_id),
        ).fetchone():
            raise ValueError('One or both transactions are already paired')
        cursor = conn.execute(
            'INSERT INTO transfer_pairs(outgoing_transaction_id,incoming_transaction_id,confirmed_at) VALUES (?,?,?)',
            (outgoing_id, incoming_id, datetime.now().isoformat(timespec='seconds')),
        )
        pair_id = int(cursor.lastrowid)
        _audit(conn, pair_id, 'confirm', None, {
            'outgoing_transaction_id': int(outgoing_id),
            'incoming_transaction_id': int(incoming_id),
            'currency': str(outgoing['currency']),
            'amount_minor': abs(int(outgoing['amount'])),
        })
        return pair_id


def unpair_transfer(conn: sqlite3.Connection, pair_id: int) -> bool:
    with _atomic(conn):
        row = conn.execute(
            'SELECT id, outgoing_transaction_id, incoming_transaction_id, confirmed_at FROM transfer_pairs WHERE id=?',
            (pair_id,),
        ).fetchone()
        if row is None:
            return False
        old = {'outgoing_transaction_id': int(row['outgoing_transaction_id']),
               'incoming_transaction_id': int(row['incoming_transaction_id']),
               'confirmed_at': row['confirmed_at']}
        _audit(conn, pair_id, 'unpair', old, None)
        conn.execute('DELETE FROM transfer_pairs WHERE id=?', (pair_id,))
        return True


def confirmed_transfer_pairs(
    conn: sqlite3.Connection, currency: str | None = 'MNT',
) -> list[dict[str, Any]]:
    clauses = []
    params: list[object] = []
    if currency:
        clauses.append("COALESCE(NULLIF(oa.currency,''),'MNT')=?")
        params.append(currency.upper())
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = conn.execute(
        f"""SELECT p.id, p.outgoing_transaction_id, p.incoming_transaction_id,
                   o.txn_datetime AS outgoing_date, i.txn_datetime AS incoming_date,
                   -o.amount AS amount_minor, COALESCE(NULLIF(oa.currency,''),'MNT') AS currency,
                   COALESCE(oa.nickname,'') AS outgoing_name, oa.account_number AS outgoing_number,
                   COALESCE(ia.nickname,'') AS incoming_name, ia.account_number AS incoming_number
            FROM transfer_pairs p
            JOIN transactions o ON o.id=p.outgoing_transaction_id
            JOIN transactions i ON i.id=p.incoming_transaction_id
            JOIN accounts oa ON oa.id=o.account_id
            JOIN accounts ia ON ia.id=i.account_id{where}
            ORDER BY p.id DESC""", params,
    ).fetchall()
    return [{
        'id': int(row['id']), 'outgoing_id': int(row['outgoing_transaction_id']),
        'incoming_id': int(row['incoming_transaction_id']),
        'outgoing_date': str(row['outgoing_date']), 'incoming_date': str(row['incoming_date']),
        'amount_minor': int(row['amount_minor']), 'currency': str(row['currency']),
        'outgoing_account': f"{row['outgoing_name']} · {_masked(row['outgoing_number'])}".strip(' ·'),
        'incoming_account': f"{row['incoming_name']} · {_masked(row['incoming_number'])}".strip(' ·'),
    } for row in rows]
