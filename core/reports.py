"""Parameterized financial report queries."""

import sqlite3

import pandas as pd

from core.db import read_sql_query


def _date_conditions(
    start: str | None,
    end: str | None,
) -> tuple[str, list[str | None]]:
    """Return inclusive, parameterized date conditions."""
    return (
        '''(c.type IS NULL OR c.type != 'transfer')
           AND (CAST(? AS TEXT) IS NULL OR substr(t.txn_datetime, 1, 10) >= ?)
           AND (CAST(? AS TEXT) IS NULL OR substr(t.txn_datetime, 1, 10) <= ?)''',
        [start, start, end, end],
    )


def monthly_summary(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Summarize non-transfer income and expenses by month in MNT."""
    conditions, params = _date_conditions(start, end)
    return read_sql_query(
        f'''SELECT substr(t.txn_datetime, 1, 7) AS month,
                   SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END)
                       / 100.0 AS income,
                   SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END)
                       / 100.0 AS expense,
                   SUM(t.amount) / 100.0 AS net
            FROM transactions AS t
            LEFT JOIN categories AS c ON c.id = t.category_id
            WHERE {conditions}
            GROUP BY substr(t.txn_datetime, 1, 7)
            ORDER BY month''',
        conn,
        params=params,
    )


def spending_by_category(
    conn: sqlite3.Connection,
    start: str | None,
    end: str | None,
) -> pd.DataFrame:
    """Aggregate outgoing non-transfer amounts by category in MNT."""
    conditions, params = _date_conditions(start, end)
    return read_sql_query(
        f'''SELECT COALESCE(c.name, 'Uncategorized') AS category,
                   SUM(-t.amount) / 100.0 AS total
            FROM transactions AS t
            LEFT JOIN categories AS c ON c.id = t.category_id
            WHERE {conditions} AND t.amount < 0
            GROUP BY c.id, c.name
            ORDER BY total DESC, category''',
        conn,
        params=params,
    )


def top_merchants(
    conn: sqlite3.Connection,
    start: str | None,
    end: str | None,
    limit: int = 10,
) -> pd.DataFrame:
    """Rank outgoing non-transfer merchants by amount in MNT."""
    conditions, params = _date_conditions(start, end)
    return read_sql_query(
        f'''SELECT COALESCE(NULLIF(t.merchant_clean, ''), 'Uncategorized')
                       AS merchant,
                   SUM(-t.amount) / 100.0 AS total,
                   COUNT(*) AS count
            FROM transactions AS t
            LEFT JOIN categories AS c ON c.id = t.category_id
            WHERE {conditions} AND t.amount < 0
            GROUP BY COALESCE(NULLIF(t.merchant_clean, ''), 'Uncategorized')
            ORDER BY total DESC, merchant
            LIMIT ?''',
        conn,
        params=[*params, max(0, int(limit))],
    )


def total_fees(
    conn: sqlite3.Connection,
    start: str | None,
    end: str | None,
) -> float:
    """Return outgoing fee-category amounts in MNT."""
    conditions, params = _date_conditions(start, end)
    row = conn.execute(
        f'''SELECT COALESCE(SUM(-t.amount), 0) / 100.0
            FROM transactions AS t
            JOIN categories AS c ON c.id = t.category_id
            WHERE {conditions}
              AND c.type = 'fee'
              AND t.amount < 0''',
        params,
    ).fetchone()
    return float(row[0])
