"""Test selectable dashboard filtering and local aggregations."""

from core.db import get_conn, init_db
from core.reports import monthly_summary
from ui.reports import (
    _load_dashboard_data,
    _monthly_summary,
    _spending_by_category,
    _spending_by_category_with_income,
    _top_merchants,
)


def _dashboard_connection():
    conn = get_conn(':memory:')
    init_db(conn)
    conn.executemany(
        '''INSERT INTO transactions
           (txn_datetime, amount, direction, raw_description, merchant_clean,
            category_id, classified_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        [
            ('2026-01-01 12:00:00', 100000, 'in', 'salary', 'Employer', 1, 'manual'),
            ('2026-01-02 12:00:00', -20000, 'out', 'market', 'Market', 9, 'rule'),
            ('2026-01-03 12:00:00', -1000, 'out', 'fee', '', 15, 'system'),
            ('2026-01-04 12:00:00', -5000, 'out', 'unknown', 'Cafe', None, 'none'),
            ('2026-01-05 12:00:00', -999999, 'out', 'transfer', 'Transfer', 16, 'manual'),
            ('2026-02-01 12:00:00', -10000, 'out', 'market', 'Market', 9, 'manual'),
        ],
    )
    conn.commit()
    return conn


def test_dashboard_aggregates_exclude_transfers_and_keep_fees_as_expense() -> None:
    conn = _dashboard_connection()
    try:
        data = _load_dashboard_data(
            conn,
            '2026-01-01',
            '2026-02-28',
            ['Бүгд'],
            'Бүгд',
        )
        monthly = _monthly_summary(data)
        assert monthly.to_dict('records') == [
            {'month': '2026-01', 'income': 1000.0, 'expense': 260.0, 'net': 740.0},
            {'month': '2026-02', 'income': 0.0, 'expense': 100.0, 'net': -100.0},
        ]
        spending = _spending_by_category(data)
        assert spending.iloc[0]['total'] == 300.0
        assert 'Transfers (own accounts)' not in spending['category'].tolist()
        shares = spending.set_index('category')['income_share_pct']
        assert shares['Groceries / Food'] == 30.0
        assert shares['Bank Fees & Charges'] == 1.0
        assert shares['Ангилалгүй'] == 5.0
        merchants = _top_merchants(data)
        assert merchants.iloc[0]['merchant'] == 'Market'
        assert int(merchants.iloc[0]['count']) == 2
    finally:
        conn.close()


def test_spending_share_uses_full_income_and_handles_no_income() -> None:
    conn = _dashboard_connection()
    try:
        data = _load_dashboard_data(
            conn,
            '2026-01-01',
            '2026-02-28',
            ['Бүгд'],
            'Бүгд',
        )
        spending = _spending_by_category_with_income(data, 2_000_000)
        shares = spending.set_index('category')['income_share_pct']
        assert shares['Groceries / Food'] == 1.5
        assert shares['Bank Fees & Charges'] == 0.05

        no_income = _spending_by_category_with_income(data, 0)
        assert no_income['income_share_pct'].isna().all()
    finally:
        conn.close()


def test_dashboard_category_and_classification_filters() -> None:
    conn = _dashboard_connection()
    try:
        groceries = _load_dashboard_data(
            conn,
            '2026-01-01',
            '2026-01-31',
            ['Groceries / Food'],
            'Гараар ангилсан',
        )
        assert len(groceries) == 0

        uncategorized = _load_dashboard_data(
            conn,
            '2026-01-01',
            '2026-01-31',
            ['Ангилалгүй'],
            'Ангилаагүй',
        )
        assert uncategorized['description'].tolist() == ['unknown']
    finally:
        conn.close()


def test_dashboard_can_analyze_one_import_including_overlapping_rows() -> None:
    conn = _dashboard_connection()
    try:
        first_import = conn.execute(
            '''INSERT INTO imports(filename, rows_parsed, imported_at)
               VALUES ('first.xlsx', 2, '2026-01-01')'''
        ).lastrowid
        second_import = conn.execute(
            '''INSERT INTO imports(filename, rows_parsed, imported_at)
               VALUES ('second.xlsx', 2, '2026-02-01')'''
        ).lastrowid
        conn.execute(
            '''INSERT INTO import_extensions(parent_import_id, child_import_id)
               VALUES (?, ?)''',
            (first_import, second_import),
        )
        third_import = conn.execute(
            '''INSERT INTO imports(filename, rows_parsed, imported_at)
               VALUES ('third.xlsx', 1, '2026-03-01')'''
        ).lastrowid
        conn.execute(
            '''INSERT INTO import_extensions(parent_import_id, child_import_id)
               VALUES (?, ?)''',
            (second_import, third_import),
        )
        first_transaction = conn.execute(
            "SELECT id FROM transactions WHERE raw_description='market' "
            "AND txn_datetime LIKE '2026-01-02%'"
        ).fetchone()[0]
        second_transaction = conn.execute(
            "SELECT id FROM transactions WHERE txn_datetime LIKE '2026-02-01%'"
        ).fetchone()[0]
        third_transaction = conn.execute(
            "SELECT id FROM transactions WHERE txn_datetime LIKE '2026-01-03%'"
        ).fetchone()[0]
        conn.executemany(
            '''INSERT INTO import_transactions(import_id, transaction_id)
               VALUES (?, ?)''',
            [
                (first_import, first_transaction),
                (second_import, first_transaction),
                (second_import, second_transaction),
                (third_import, third_transaction),
            ],
        )
        conn.commit()

        selected = _load_dashboard_data(
            conn,
            '2026-01-01',
            '2026-02-28',
            ['Бүгд'],
            'Бүгд',
            import_id=first_import,
        )

        assert set(selected['id']) == {
            first_transaction,
            second_transaction,
            third_transaction,
        }
    finally:
        conn.close()



def test_transaction_list_and_report_totals_share_scope_and_end_date() -> None:
    conn = _dashboard_connection()
    try:
        conn.execute(
            '''INSERT INTO transactions
               (txn_datetime, amount, direction, raw_description, merchant_clean,
                category_id, classified_by)
               VALUES ('2026-02-28 23:59:59', -1250, 'out', 'late expense', 'Late', 9, 'manual')'''
        )
        conn.commit()
        rows = _load_dashboard_data(
            conn, '2026-02-28', '2026-02-28', ['Бүгд'], 'Бүгд',
            currency='MNT',
        )
        assert rows['date'].tolist() == ['2026-02-28 23:59:59']
        list_expense = int(-rows.loc[
            (rows['amount'] < 0) & (rows['category_type'] != 'transfer'), 'amount'
        ].sum())
        report = monthly_summary(conn, '2026-02-28', '2026-02-28', currency='MNT')
        assert list_expense == int(round(report.iloc[0]['expense'] * 100))
    finally:
        conn.close()
