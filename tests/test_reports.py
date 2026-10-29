"""Tests for date-bounded financial report queries."""

import sqlite3

from core.db import get_conn, init_db
from core.reports import (
    monthly_summary,
    spending_by_category,
    top_merchants,
    total_fees,
)


def _report_connection() -> sqlite3.Connection:
    """Create a database with income, spending, fees and transfers."""
    conn = get_conn(':memory:')
    init_db(conn)
    conn.executemany(
        '''INSERT INTO transactions
           (txn_datetime, amount, category_id, merchant_clean)
           VALUES (?, ?, ?, ?)''',
        [
            ('2026-01-01 12:00:00', 100000, 1, 'salary'),
            ('2026-01-02 12:00:00', -20000, 6, 'store'),
            ('2026-01-03 12:00:00', -1000, 15, ''),
            ('2026-01-04 12:00:00', -5000, None, 'cafe'),
            ('2026-01-05 12:00:00', -999999, 16, 'transfer'),
            ('2026-02-01 12:00:00', -10000, 6, 'store'),
            ('2026-02-02 12:00:00', -10000, 6, 'market'),
        ],
    )
    conn.commit()
    return conn


def test_monthly_summary_converts_amounts_and_excludes_transfers() -> None:
    conn = _report_connection()
    try:
        summary = monthly_summary(conn)

        assert list(summary.columns) == ['month', 'income', 'expense', 'net']
        assert summary.to_dict('records') == [
            {'month': '2026-01', 'income': 1000.0, 'expense': 260.0, 'net': 740.0},
            {'month': '2026-02', 'income': 0.0, 'expense': 200.0, 'net': -200.0},
        ]
        january = monthly_summary(conn, '2026-01-01', '2026-01-31')
        assert january.to_dict('records') == [
            {'month': '2026-01', 'income': 1000.0, 'expense': 260.0, 'net': 740.0},
        ]
    finally:
        conn.close()


def test_category_spending_includes_uncategorized_and_excludes_transfers() -> None:
    conn = _report_connection()
    try:
        spending = spending_by_category(conn, None, None)

        assert list(spending.columns) == ['category', 'total']
        totals = spending.set_index('category')['total']
        assert totals['Uncategorized'] == 50.0
        assert totals['Bank Fees & Charges'] == 10.0
        assert spending.iloc[0]['total'] == 400.0
        assert 'Transfers (own accounts)' not in set(spending['category'])
    finally:
        conn.close()


def test_top_merchants_orders_outflows_and_applies_limit() -> None:
    conn = _report_connection()
    try:
        merchants = top_merchants(conn, None, None)

        assert list(merchants.columns) == ['merchant', 'total', 'count']
        assert merchants['merchant'].tolist() == [
            'store', 'market', 'cafe', 'Uncategorized',
        ]
        assert merchants['total'].tolist() == [300.0, 100.0, 50.0, 10.0]
        assert merchants['count'].tolist() == [2, 1, 1, 1]
        assert top_merchants(conn, None, None, limit=2)['merchant'].tolist() == [
            'store', 'market',
        ]
    finally:
        conn.close()


def test_fee_total_and_empty_date_range() -> None:
    conn = _report_connection()
    try:
        assert total_fees(conn, None, None) == 10.0
        assert monthly_summary(conn, '2030-01-01', None).empty
        assert spending_by_category(conn, '2030-01-01', None).empty
        assert top_merchants(conn, '2030-01-01', None).empty
        assert total_fees(conn, '2030-01-01', None) == 0.0
    finally:
        conn.close()



def test_report_scope_separates_accounts_and_currencies_and_includes_end_day() -> None:
    conn = _report_connection()
    try:
        conn.executemany(
            'INSERT INTO accounts(id, account_number, currency) VALUES (?,?,?)',
            [(21, '111100000021', 'MNT'), (22, '222200000022', 'MNT'),
             (23, '333300000023', 'USD')],
        )
        conn.executemany(
            '''INSERT INTO transactions(account_id, txn_datetime, amount, direction, category_id)
               VALUES (?,?,?,?,?)''',
            [(21, '2026-11-30 23:59:59', -12500, 'out', 6),
             (22, '2026-11-30 09:00:00', -7000, 'out', 6),
             (23, '2026-11-30 23:59:59', -3000, 'out', 6)],
        )
        conn.commit()
        first = monthly_summary(conn, '2026-11-30', '2026-11-30', 21, 'MNT')
        assert first.to_dict('records') == [
            {'month': '2026-11', 'income': 0.0, 'expense': 125.0, 'net': -125.0}
        ]
        second = monthly_summary(conn, '2026-11-30', '2026-11-30', 22, 'MNT')
        assert second.iloc[0]['expense'] == 70.0
        usd = monthly_summary(conn, '2026-11-30', '2026-11-30', None, 'USD')
        assert usd.iloc[0]['expense'] == 30.0
        assert total_fees(conn, '2026-11-30', '2026-11-30', 21, 'MNT') == 0.0
    finally:
        conn.close()
