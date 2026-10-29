"""Shared, parameterized reporting scopes and financial aggregates."""

import sqlite3

import pandas as pd

from core.db import read_sql_query


def transaction_scope(
    start: str | None = None,
    end: str | None = None,
    account_id: int | None = None,
    currency: str | None = 'MNT',
    *,
    txn_alias: str = 't',
    account_alias: str = 'a',
) -> tuple[list[str], list[object]]:
    """Build the common inclusive-date/account/currency transaction scope."""
    clauses: list[str] = []
    params: list[object] = []
    if start:
        clauses.append(f'substr({txn_alias}.txn_datetime, 1, 10) >= ?')
        params.append(start[:10])
    if end:
        clauses.append(f'substr({txn_alias}.txn_datetime, 1, 10) <= ?')
        params.append(end[:10])
    if account_id is not None:
        clauses.append(f'{txn_alias}.account_id = ?')
        params.append(int(account_id))
    selected_currency = (currency or 'MNT').upper()
    clauses.append(
        f"COALESCE(NULLIF({account_alias}.currency, ''), 'MNT') = ?"
    )
    params.append(selected_currency)
    return clauses, params


def available_currencies(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT COALESCE(NULLIF(a.currency, ''), 'MNT') AS currency
           FROM transactions AS t
           LEFT JOIN accounts AS a ON a.id=t.account_id
           ORDER BY currency"""
    ).fetchall()
    values = [str(row['currency']).upper() for row in rows if row['currency']]
    return values or ['MNT']


def available_accounts(
    conn: sqlite3.Connection, currency: str | None = None,
) -> list[dict[str, object]]:
    sql = """SELECT DISTINCT a.id, a.account_number, a.nickname,
                            COALESCE(NULLIF(a.currency, ''), 'MNT') AS currency
             FROM accounts AS a JOIN transactions AS t ON t.account_id=a.id
             WHERE 1=1"""
    params: list[object] = []
    if currency:
        sql += " AND COALESCE(NULLIF(a.currency, ''), 'MNT')=?"
        params.append(currency.upper())
    sql += ' ORDER BY a.nickname, a.account_number, a.id'
    options: list[dict[str, object]] = []
    for row in conn.execute(sql, params):
        number = str(row['account_number'] or '')
        masked = ('*' * max(0, len(number) - 4) + number[-4:]) if number else 'дугааргүй'
        name = str(row['nickname'] or '').strip()
        label = f'{name} · {masked} · #{int(row["id"])}' if name else f'{masked} · #{int(row["id"])}'
        options.append({'id': int(row['id']), 'label': label,
                        'currency': str(row['currency']).upper()})
    return options


def _where(
    start: str | None, end: str | None, account_id: int | None,
    currency: str | None,
) -> tuple[str, list[object]]:
    clauses, params = transaction_scope(start, end, account_id, currency)
    clauses.extend([
        "(c.type IS NULL OR c.type != 'transfer')",
        "NOT EXISTS (SELECT 1 FROM transfer_pairs AS tp WHERE tp.outgoing_transaction_id=t.id OR tp.incoming_transaction_id=t.id)",
    ])
    return ' AND '.join(clauses) if clauses else '1=1', params


def monthly_summary(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    account_id: int | None = None,
    currency: str | None = 'MNT',
) -> pd.DataFrame:
    """Summarize non-transfer income and expenses in the selected currency."""
    where, params = _where(start, end, account_id, currency)
    return read_sql_query(
        f"""SELECT substr(t.txn_datetime, 1, 7) AS month,
                   SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END)
                       / 100.0 AS income,
                   SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END)
                       / 100.0 AS expense,
                   SUM(t.amount) / 100.0 AS net
            FROM transactions AS t
            LEFT JOIN categories AS c ON c.id=t.category_id
            LEFT JOIN accounts AS a ON a.id=t.account_id
            WHERE {where}
            GROUP BY substr(t.txn_datetime, 1, 7)
            ORDER BY month""",
        conn, params=params,
    )


def spending_by_category(
    conn: sqlite3.Connection, start: str | None, end: str | None,
    account_id: int | None = None, currency: str | None = 'MNT',
) -> pd.DataFrame:
    """Aggregate outgoing amounts, retaining fees and uncategorized spending."""
    where, params = _where(start, end, account_id, currency)
    return read_sql_query(
        f"""SELECT COALESCE(c.name, 'Uncategorized') AS category,
                   SUM(-t.amount) / 100.0 AS total
            FROM transactions AS t
            LEFT JOIN categories AS c ON c.id=t.category_id
            LEFT JOIN accounts AS a ON a.id=t.account_id
            WHERE {where} AND t.amount < 0
            GROUP BY c.id, c.name
            ORDER BY total DESC, category""",
        conn, params=params,
    )


def top_merchants(
    conn: sqlite3.Connection, start: str | None, end: str | None,
    limit: int = 10, account_id: int | None = None,
    currency: str | None = 'MNT',
) -> pd.DataFrame:
    """Rank outgoing non-transfer merchants within one reporting scope."""
    where, params = _where(start, end, account_id, currency)
    return read_sql_query(
        f"""SELECT COALESCE(NULLIF(t.merchant_clean, ''), 'Uncategorized')
                       AS merchant,
                   SUM(-t.amount) / 100.0 AS total, COUNT(*) AS count
            FROM transactions AS t
            LEFT JOIN categories AS c ON c.id=t.category_id
            LEFT JOIN accounts AS a ON a.id=t.account_id
            WHERE {where} AND t.amount < 0
            GROUP BY COALESCE(NULLIF(t.merchant_clean, ''), 'Uncategorized')
            ORDER BY total DESC, merchant
            LIMIT ?""",
        conn, params=[*params, max(0, int(limit))],
    )


def total_fees(
    conn: sqlite3.Connection, start: str | None, end: str | None,
    account_id: int | None = None, currency: str | None = 'MNT',
) -> float:
    """Return outgoing fee amounts in major units of the selected currency."""
    clauses, params = transaction_scope(start, end, account_id, currency)
    clauses.extend([
        "c.type = 'fee'", 't.amount < 0',
        "NOT EXISTS (SELECT 1 FROM transfer_pairs AS tp WHERE tp.outgoing_transaction_id=t.id OR tp.incoming_transaction_id=t.id)",
    ])
    row = conn.execute(
        f"""SELECT COALESCE(SUM(-t.amount), 0)
            FROM transactions AS t
            JOIN categories AS c ON c.id=t.category_id
            LEFT JOIN accounts AS a ON a.id=t.account_id
            WHERE {' AND '.join(clauses)}""",
        params,
    ).fetchone()
    return int(row[0]) / 100
