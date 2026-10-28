"""Monthly category budgets and actual spending."""

import sqlite3
from datetime import date, datetime

import pandas as pd

from core.db import read_sql_query


def next_month_start(today: date | None = None) -> date:
    """Return the first day of the month after today."""
    current = today or date.today()
    if current.month == 12:
        return date(current.year + 1, 1, 1)
    return date(current.year, current.month + 1, 1)


def get_budget_categories(
    conn: sqlite3.Connection,
    budget_month: str,
) -> list[sqlite3.Row]:
    """Return active expense and fee categories with saved budgets."""
    return conn.execute(
        '''SELECT c.id, c.name, c.type, b.amount
           FROM categories AS c
           LEFT JOIN budgets AS b
             ON b.category_id=c.id AND b.budget_month=?
           WHERE c.is_active=1 AND c.type IN ('expense', 'fee')
           ORDER BY c.type, c.name''',
        (budget_month,),
    ).fetchall()


def save_budgets(
    conn: sqlite3.Connection,
    budget_month: str,
    amounts: dict[int, int | None],
) -> None:
    """Save or remove monthly budgets in integer minor units."""
    try:
        date.fromisoformat(f'{budget_month}-01')
    except ValueError as exc:
        raise ValueError('Budget month must use YYYY-MM format.') from exc

    validated: list[tuple[int, int | None]] = []
    for category_id, amount in amounts.items():
        category = conn.execute(
            '''SELECT type, is_active FROM categories WHERE id=?''',
            (category_id,),
        ).fetchone()
        if (
            category is None
            or not category['is_active']
            or category['type'] not in ('expense', 'fee')
        ):
            raise ValueError('Budgets can only be set for active expense categories.')
        if amount is not None and (
            isinstance(amount, bool) or not isinstance(amount, int) or amount < 0
        ):
            raise ValueError('Budget amounts must be non-negative integer minor units.')
        validated.append((category_id, amount))

    updated_at = datetime.now().isoformat(timespec='seconds')
    with conn:
        for category_id, amount in validated:
            if amount is None:
                conn.execute(
                    '''DELETE FROM budgets
                       WHERE budget_month=? AND category_id=?''',
                    (budget_month, category_id),
                )
            else:
                conn.execute(
                    '''INSERT INTO budgets(
                           budget_month, category_id, amount, updated_at
                       ) VALUES (?, ?, ?, ?)
                       ON CONFLICT(budget_month, category_id) DO UPDATE SET
                           amount=excluded.amount,
                           updated_at=excluded.updated_at
                       RETURNING budget_month''',
                    (budget_month, category_id, amount, updated_at),
                )


def budget_comparison(
    conn: sqlite3.Connection,
    budget_month: str,
) -> pd.DataFrame:
    """Compare category budgets with actual spending for one month."""
    result = read_sql_query(
        '''SELECT c.id AS category_id, c.name AS category,
                  c.type AS category_type,
                  COALESCE(b.amount, 0) AS budget_minor,
                  COALESCE(SUM(-t.amount), 0) AS actual_minor
           FROM categories AS c
           LEFT JOIN budgets AS b
             ON b.category_id=c.id AND b.budget_month=?
           LEFT JOIN transactions AS t
             ON t.category_id=c.id
            AND substr(t.txn_datetime, 1, 7)=?
            AND t.amount < 0
           WHERE c.type IN ('expense', 'fee')
           GROUP BY c.id, c.name, c.type, b.amount
           ORDER BY c.type, c.name''',
        conn,
        params=[budget_month, budget_month],
    )
    uncategorized = conn.execute(
        '''SELECT COALESCE(SUM(-amount), 0)
           FROM transactions
           WHERE category_id IS NULL
             AND amount < 0
             AND substr(txn_datetime, 1, 7)=?''',
        (budget_month,),
    ).fetchone()[0]
    if uncategorized:
        result.loc[len(result)] = {
            'category_id': None,
            'category': 'Ангилалгүй',
            'category_type': 'expense',
            'budget_minor': 0,
            'actual_minor': int(uncategorized),
        }
    if result.empty:
        result['remaining_minor'] = pd.Series(dtype='int64')
        result['used_percent'] = pd.Series(dtype='float64')
        return result
    result['budget_minor'] = result['budget_minor'].astype('int64')
    result['actual_minor'] = result['actual_minor'].astype('int64')
    result['remaining_minor'] = result['budget_minor'] - result['actual_minor']
    result['used_percent'] = result.apply(
        lambda row: (
            row['actual_minor'] / row['budget_minor'] * 100
            if row['budget_minor'] > 0
            else None
        ),
        axis=1,
    )
    return result
