"""Monthly category budgets and actual spending."""

import calendar
import sqlite3
from datetime import date, datetime

import pandas as pd

from core.db import read_sql_query
from core.reports import transaction_scope


def next_month_start(today: date | None = None) -> date:
    """Return the first day of the month after today."""
    current = today or date.today()
    if current.month == 12:
        return date(current.year + 1, 1, 1)
    return date(current.year, current.month + 1, 1)


def get_budget_categories(
    conn: sqlite3.Connection,
    budget_month: str,
    currency: str = 'MNT',
    account_id: int | None = None,
) -> list[sqlite3.Row]:
    """Return active expense/fee budgets for the selected currency/account scope."""
    account_scope = 'all' if account_id is None else str(int(account_id))
    return conn.execute(
        '''SELECT c.id, c.name, c.type, b.amount
           FROM categories AS c
           LEFT JOIN budgets AS b
             ON b.category_id=c.id AND b.budget_month=?
            AND b.currency=? AND b.account_scope=?
           WHERE c.is_active=1 AND c.type IN ('expense', 'fee')
           ORDER BY c.type, c.name''',
        (budget_month, currency.upper(), account_scope),
    ).fetchall()


def save_budgets(
    conn: sqlite3.Connection,
    budget_month: str,
    amounts: dict[int, int | None],
    account_id: int | None = None,
    currency: str = 'MNT',
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

    currency = currency.strip().upper()
    if not currency or len(currency) > 8:
        raise ValueError('Currency code is invalid.')
    if account_id is None:
        account_scope = 'all'
    else:
        account = conn.execute(
            "SELECT COALESCE(NULLIF(currency,''),'MNT') AS currency FROM accounts WHERE id=?",
            (int(account_id),),
        ).fetchone()
        if account is None or str(account['currency']).upper() != currency:
            raise ValueError('Selected account does not use this currency.')
        account_scope = str(int(account_id))
    updated_at = datetime.now().isoformat(timespec='seconds')
    with conn:
        for category_id, amount in validated:
            if amount is None:
                conn.execute(
                    '''DELETE FROM budgets
                       WHERE budget_month=? AND category_id=?
                         AND currency=? AND account_scope=?''',
                    (budget_month, category_id, currency, account_scope),
                )
            else:
                conn.execute(
                    '''INSERT INTO budgets(
                           budget_month, category_id, amount, updated_at,
                           currency, account_scope
                       ) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(budget_month, category_id, currency, account_scope) DO UPDATE SET
                           amount=excluded.amount,
                           updated_at=excluded.updated_at
                       RETURNING budget_month''',
                    (budget_month, category_id, amount, updated_at,
                     currency, account_scope),
                )



def previous_budget_month(budget_month: str) -> str:
    """Return the calendar month immediately before the supplied month."""
    try:
        selected = date.fromisoformat(f'{budget_month}-01')
    except ValueError as exc:
        raise ValueError('Budget month must use YYYY-MM format.') from exc
    year, month = (selected.year - 1, 12) if selected.month == 1 else (selected.year, selected.month - 1)
    return f'{year:04d}-{month:02d}'


def preview_budget_copy(
    conn: sqlite3.Connection,
    source_month: str,
    target_month: str,
    account_id: int | None = None,
    currency: str = 'MNT',
) -> list[dict[str, object]]:
    """Preview source budgets and identify target conflicts without writing."""
    previous_budget_month(source_month)
    previous_budget_month(target_month)
    account_scope = 'all' if account_id is None else str(int(account_id))
    rows = conn.execute(
        '''SELECT source.category_id, c.name AS category, source.amount AS source_amount,
                  target.amount AS target_amount,
                  CASE WHEN target.amount IS NULL THEN 0 ELSE 1 END AS has_target
           FROM budgets AS source
           JOIN categories AS c ON c.id=source.category_id
           LEFT JOIN budgets AS target
             ON target.budget_month=? AND target.category_id=source.category_id
            AND target.currency=source.currency AND target.account_scope=source.account_scope
           WHERE source.budget_month=? AND source.currency=? AND source.account_scope=?
             AND c.is_active=1 AND c.type IN ('expense','fee')
           ORDER BY c.name''',
        (target_month, source_month, currency.strip().upper(), account_scope),
    ).fetchall()
    return [dict(row) for row in rows]


def copy_budget_month(
    conn: sqlite3.Connection,
    source_month: str,
    target_month: str,
    account_id: int | None = None,
    currency: str = 'MNT',
) -> dict[str, int]:
    """Copy only missing targets; existing target values are always preserved."""
    preview = preview_budget_copy(conn, source_month, target_month, account_id, currency)
    account_scope = 'all' if account_id is None else str(int(account_id))
    copied = 0
    with conn:
        for row in preview:
            if row['has_target']:
                continue
            cursor = conn.execute(
                '''INSERT INTO budgets(budget_month, category_id, amount, updated_at, currency, account_scope)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(budget_month, category_id, currency, account_scope) DO NOTHING''',
                (target_month, int(row['category_id']), int(row['source_amount']),
                 datetime.now().isoformat(timespec='seconds'), currency.strip().upper(), account_scope),
            )
            copied += max(0, int(cursor.rowcount))
    return {'copied': copied, 'skipped_conflicts': len(preview) - copied}


def budget_comparison(
    conn: sqlite3.Connection,
    budget_month: str,
    account_id: int | None = None,
    currency: str | None = 'MNT',
) -> pd.DataFrame:
    """Compare monthly budgets with actual scoped non-transfer spending."""
    month_start = date.fromisoformat(f'{budget_month}-01')
    last_day = date(
        month_start.year, month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    ).isoformat()
    clauses, params = transaction_scope(
        month_start.isoformat(), last_day, account_id, currency,
    )
    clauses.extend([
        't.amount < 0',
        "(c.type IN ('expense','fee') OR t.category_id IS NULL)",
        "(c.type IS NULL OR c.type!='transfer')",
        "NOT EXISTS (SELECT 1 FROM transfer_pairs AS tp WHERE tp.outgoing_transaction_id=t.id OR tp.incoming_transaction_id=t.id)",
    ])
    actual_rows = conn.execute(
        f'''SELECT t.category_id, COALESCE(SUM(-t.amount),0) AS actual_minor
            FROM transactions AS t
            LEFT JOIN categories AS c ON c.id=t.category_id
            LEFT JOIN accounts AS a ON a.id=t.account_id
            WHERE {' AND '.join(clauses)}
            GROUP BY t.category_id''',
        params,
    ).fetchall()
    actuals = {row['category_id']: int(row['actual_minor']) for row in actual_rows}
    result = read_sql_query(
        '''SELECT c.id AS category_id, c.name AS category,
                  c.type AS category_type,
                  COALESCE(b.amount, 0) AS budget_minor,
                  0 AS actual_minor
           FROM categories AS c
           LEFT JOIN budgets AS b
             ON b.category_id=c.id AND b.budget_month=?
            AND b.currency=? AND b.account_scope=?
           WHERE c.type IN ('expense', 'fee')
           GROUP BY c.id, c.name, c.type, b.amount
           ORDER BY c.type, c.name''',
        conn,
        params=[budget_month, (currency or 'MNT').upper(),
                'all' if account_id is None else str(int(account_id))],
    )
    result['actual_minor'] = result['category_id'].map(
        lambda category_id: actuals.pop(category_id, 0)
    ).astype('int64')
    uncategorized = actuals.pop(None, 0)
    if uncategorized:
        result.loc[len(result)] = {
            'category_id': None, 'category': 'Ангилалгүй',
            'category_type': 'expense', 'budget_minor': 0,
            'actual_minor': int(uncategorized),
        }
    if result.empty:
        result['remaining_minor'] = pd.Series(dtype='int64')
        result['used_percent'] = pd.Series(dtype='float64')
        return result
    result['budget_minor'] = result['budget_minor'].astype('int64')
    result['actual_minor'] = result['actual_minor'].astype('int64')
    result['remaining_minor'] = result['budget_minor'] - result['actual_minor']
    uncategorized_mask = result['category_id'].isna()
    result.loc[uncategorized_mask, 'remaining_minor'] = 0
    result['used_percent'] = result.apply(
        lambda row: (
            row['actual_minor'] / row['budget_minor'] * 100
            if row['budget_minor'] > 0
            else None
        ),
        axis=1,
    )
    return result
