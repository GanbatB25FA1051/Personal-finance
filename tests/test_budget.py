"""Test monthly category budgets and actual spending."""

from datetime import date

import pytest

from core.budget import (
    budget_comparison,
    get_budget_categories,
    next_month_start,
    save_budgets,
    previous_budget_month, preview_budget_copy, copy_budget_month,
)
from core.db import get_conn, init_db


def _budget_connection():
    conn = get_conn(':memory:')
    init_db(conn)
    groceries_id = conn.execute(
        "SELECT id FROM categories WHERE name='Groceries / Food'"
    ).fetchone()[0]
    salary_id = conn.execute(
        "SELECT id FROM categories WHERE name='Earned'"
    ).fetchone()[0]
    conn.executemany(
        '''INSERT INTO transactions(
               txn_datetime, amount, direction, category_id
           ) VALUES (?, ?, ?, ?)''',
        [
            ('2026-11-02 10:00:00', -25000000, 'out', groceries_id),
            ('2026-11-10 10:00:00', -15000000, 'out', groceries_id),
            ('2026-11-12 10:00:00', -1000000, 'out', None),
            ('2026-11-15 10:00:00', 200000000, 'in', salary_id),
            ('2026-12-01 10:00:00', -90000000, 'out', groceries_id),
        ],
    )
    conn.commit()
    return conn, groceries_id, salary_id


@pytest.mark.parametrize(
    ('today', 'expected'),
    [
        (date(2026, 10, 28), date(2026, 11, 1)),
        (date(2026, 12, 31), date(2027, 1, 1)),
    ],
)
def test_next_month_start(today: date, expected: date) -> None:
    assert next_month_start(today) == expected


def test_budget_comparison_uses_month_and_tracks_uncategorized() -> None:
    conn, groceries_id, _ = _budget_connection()
    try:
        save_budgets(conn, '2026-11', {groceries_id: 50000000})

        comparison = budget_comparison(conn, '2026-11')
        groceries = comparison.set_index('category').loc['Groceries / Food']
        uncategorized = comparison.set_index('category').loc['Ангилалгүй']

        assert groceries['budget_minor'] == 50000000
        assert groceries['actual_minor'] == 40000000
        assert groceries['remaining_minor'] == 10000000
        assert groceries['used_percent'] == 80.0
        assert uncategorized['actual_minor'] == 1000000
        assert 'Earned' not in set(comparison['category'])
        assert budget_comparison(conn, '2026-12').set_index('category').loc[
            'Groceries / Food', 'actual_minor'
        ] == 90000000
    finally:
        conn.close()


def test_budget_can_be_updated_and_removed() -> None:
    conn, groceries_id, _ = _budget_connection()
    try:
        save_budgets(conn, '2026-11', {groceries_id: 50000000})
        save_budgets(conn, '2026-11', {groceries_id: 60000000})
        saved = {
            row['id']: row['amount']
            for row in get_budget_categories(conn, '2026-11')
        }
        assert saved[groceries_id] == 60000000

        save_budgets(conn, '2026-11', {groceries_id: None})
        saved = {
            row['id']: row['amount']
            for row in get_budget_categories(conn, '2026-11')
        }
        assert saved[groceries_id] is None
    finally:
        conn.close()


def test_budget_rejects_invalid_month_and_non_expense_categories() -> None:
    conn, groceries_id, salary_id = _budget_connection()
    try:
        with pytest.raises(ValueError, match='YYYY-MM'):
            save_budgets(conn, '2026-13', {groceries_id: 100})
        with pytest.raises(ValueError, match='active expense'):
            save_budgets(conn, '2026-11', {salary_id: 100})
        with pytest.raises(ValueError, match='non-negative'):
            save_budgets(conn, '2026-11', {groceries_id: -100})
    finally:
        conn.close()



def test_budget_scope_separates_currency_and_account() -> None:
    conn, groceries_id, _ = _budget_connection()
    try:
        conn.executemany(
            'INSERT INTO accounts(id, account_number, currency) VALUES (?,?,?)',
            [(31, '111100000031', 'MNT'), (32, '222200000032', 'USD')],
        )
        conn.executemany(
            '''INSERT INTO transactions(account_id, txn_datetime, amount, direction, category_id)
               VALUES (?,?,?,?,?)''',
            [(31, '2026-11-30 23:59:00', -1000, 'out', groceries_id),
             (32, '2026-11-30 23:59:00', -2500, 'out', groceries_id)],
        )
        conn.commit()
        save_budgets(conn, '2026-11', {groceries_id: 5000}, 31, 'MNT')
        save_budgets(conn, '2026-11', {groceries_id: 9000}, 32, 'USD')
        mnt = budget_comparison(conn, '2026-11', 31, 'MNT').set_index('category')
        usd = budget_comparison(conn, '2026-11', 32, 'USD').set_index('category')
        assert mnt.loc['Groceries / Food', 'budget_minor'] == 5000
        assert mnt.loc['Groceries / Food', 'actual_minor'] == 1000
        assert usd.loc['Groceries / Food', 'budget_minor'] == 9000
        assert usd.loc['Groceries / Food', 'actual_minor'] == 2500
        assert next(row for row in get_budget_categories(conn, '2026-11', 'MNT', 31) if row['id'] == groceries_id)['amount'] == 5000
    finally:
        conn.close()


def test_previous_month_rolls_over_year_and_copy_preserves_conflicts() -> None:
    conn, groceries_id, _ = _budget_connection()
    try:
        assert previous_budget_month('2027-01') == '2026-12'
        save_budgets(conn, '2026-12', {groceries_id: 50000})
        save_budgets(conn, '2027-01', {groceries_id: 70000})
        preview = preview_budget_copy(conn, '2026-12', '2027-01')
        assert preview[0]['has_target'] == 1
        result = copy_budget_month(conn, '2026-12', '2027-01')
        assert result == {'copied': 0, 'skipped_conflicts': 1}
        current = {row['id']: row['amount'] for row in get_budget_categories(conn, '2027-01')}
        assert current[groceries_id] == 70000
    finally:
        conn.close()


def test_copy_budget_month_copies_missing_targets_across_december_january() -> None:
    conn, groceries_id, _ = _budget_connection()
    try:
        save_budgets(conn, '2026-12', {groceries_id: 50000})
        assert preview_budget_copy(conn, '2026-12', '2027-01')[0]['has_target'] == 0
        assert copy_budget_month(conn, '2026-12', '2027-01') == {
            'copied': 1, 'skipped_conflicts': 0,
        }
        copied = {row['id']: row['amount'] for row in get_budget_categories(conn, '2027-01')}
        assert copied[groceries_id] == 50000
    finally:
        conn.close()


def test_budget_actual_includes_leap_day_and_uncategorized_has_no_remaining_ratio() -> None:
    conn, groceries_id, _ = _budget_connection()
    try:
        conn.execute(
            '''INSERT INTO transactions(txn_datetime, amount, direction, category_id)
               VALUES ('2028-02-29 23:59:00', -2500, 'out', ?)''',
            (groceries_id,),
        )
        conn.execute(
            '''INSERT INTO transactions(txn_datetime, amount, direction, category_id)
               VALUES ('2028-02-29 23:58:00', -1000, 'out', NULL)'''
        )
        conn.commit()
        save_budgets(conn, '2028-02', {groceries_id: 10000})
        result = budget_comparison(conn, '2028-02').set_index('category')
        assert result.loc['Groceries / Food', 'actual_minor'] == 2500
        assert result.loc['Ангилалгүй', 'actual_minor'] == 1000
        assert result.loc['Ангилалгүй', 'remaining_minor'] == 0
        import pandas as pd
        assert pd.isna(result.loc['Ангилалгүй', 'used_percent'])
    finally:
        conn.close()
