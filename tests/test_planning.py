"""Recurring planned commitment tests; plans never create actual rows."""

import pytest

from core.budget import budget_comparison
from core.db import get_conn, init_db
from core.planning import (
    confirm_planned_payment_match,
    planned_payments_for_month,
    save_recurring_payment,
)


def _connection():
    conn = get_conn(':memory:')
    init_db(conn)
    return conn


def test_planned_recurring_payment_reconciles_after_confirmed_actual_match():
    conn = _connection()
    try:
        category_id = conn.execute(
            "SELECT id FROM categories WHERE name='Groceries / Food'"
        ).fetchone()[0]
        payment_id = save_recurring_payment(
            conn, 'Түрээс', 10000, 'MNT', 31, '2028-02', None, None, category_id,
        )
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0
        occurrence = planned_payments_for_month(conn, '2028-02')[0]
        assert occurrence['due_date'] == '2028-02-29'
        assert occurrence['unmatched_planned_minor'] == 10000

        transaction_id = conn.execute(
            '''INSERT INTO transactions(txn_datetime, amount, direction, category_id)
               VALUES ('2028-02-18 10:00:00', -9800, 'out', ?) RETURNING id''',
            (category_id,),
        ).fetchone()[0]
        actual_before = int(budget_comparison(conn, '2028-02').set_index('category').loc[
            'Groceries / Food', 'actual_minor'
        ])
        confirm_planned_payment_match(conn, payment_id, '2028-02', transaction_id)
        occurrence = planned_payments_for_month(conn, '2028-02')[0]
        actual_after = int(budget_comparison(conn, '2028-02').set_index('category').loc[
            'Groceries / Food', 'actual_minor'
        ])
        assert occurrence['is_matched'] is True
        assert occurrence['unmatched_planned_minor'] == 0
        assert actual_before == actual_after == 9800
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 1

        other_id = save_recurring_payment(
            conn, 'Өөр үйлчилгээ', 5000, 'MNT', 10, '2028-02', None, None, category_id,
        )
        with pytest.raises(ValueError, match='аль хэдийн холбогдсон'):
            confirm_planned_payment_match(conn, other_id, '2028-02', transaction_id)
    finally:
        conn.close()


def test_recurring_match_rejects_wrong_month_and_invalid_payment_dates():
    conn = _connection()
    try:
        category_id = conn.execute(
            "SELECT id FROM categories WHERE name='Groceries / Food'"
        ).fetchone()[0]
        with pytest.raises(ValueError, match='0-ээс их'):
            save_recurring_payment(conn, 'Буруу', 0, 'MNT', 1, '2026-11')
        with pytest.raises(ValueError, match='өмнө'):
            save_recurring_payment(conn, 'Буруу', 100, 'MNT', 31, '2026-12', '2026-11')
        payment_id = save_recurring_payment(
            conn, 'Төлбөр', 100, 'MNT', 5, '2026-11', None, None, category_id,
        )
        transaction_id = conn.execute(
            '''INSERT INTO transactions(txn_datetime, amount, direction, category_id)
               VALUES ('2026-11-04 10:00:00', -100, 'out', ?) RETURNING id''',
            (category_id,),
        ).fetchone()[0]
        with pytest.raises(ValueError, match='хугацаанд багтахгүй'):
            confirm_planned_payment_match(conn, payment_id, '2026-10', transaction_id)
    finally:
        conn.close()
