import pytest

from core.budget import budget_comparison
from core.db import get_conn, init_db
from core.reports import monthly_summary, total_fees
from core.transfers import (

    confirm_transfer_pair,
    confirmed_transfer_pairs,
    transfer_candidates,
    unpair_transfer,
)
from ui.transactions import _render_transfer_tools


def _connection():
    conn = get_conn(':memory:')
    init_db(conn)
    conn.executemany(
        'INSERT INTO accounts(id, account_number, currency, nickname) VALUES (?,?,?,?)',
        [(1, '111100000001', 'MNT', 'Хадгаламж'),
         (2, '222200000002', 'MNT', 'Карт'),
         (3, '333300000003', 'MNT', 'Нэмэлт данс')],
    )
    conn.executemany(
        '''INSERT INTO transactions
           (id, account_id, txn_datetime, amount, direction, raw_description,
            counterparty_account, category_id, classified_by)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        [
            (1, 1, '2026-11-15 23:59:00', -50000, 'out', 'own transfer', '222200000002', None, 'none'),
            (2, 2, '2026-11-15 08:00:00', 50000, 'in', 'incoming', '111100000001', None, 'none'),
            (3, 3, '2026-11-15 13:00:00', 50000, 'in', 'second candidate', None, None, 'none'),
            (4, 1, '2026-11-15 16:00:00', -1000, 'out', 'monthly fee', None, 15, 'system'),
        ],
    )
    conn.commit()
    return conn


def test_candidate_is_only_a_suggestion_and_confirmation_excludes_pair() -> None:
    conn = _connection()
    try:
        candidates = transfer_candidates(conn, '2026-11-15', '2026-11-15')
        assert {(row['outgoing_id'], row['incoming_id']) for row in candidates} == {(1, 2), (1, 3)}
        assert next(row for row in candidates if row['incoming_id'] == 2)['counterpart_match'] is True
        assert next(row for row in candidates if row['incoming_id'] == 3)['counterpart_match'] is None
        assert conn.execute('SELECT COUNT(*) FROM transfer_pairs').fetchone()[0] == 0

        before = monthly_summary(conn, '2026-11-15', '2026-11-15').iloc[0]
        assert before['income'] == 1000.0
        assert before['expense'] == 510.0

        pair_id = confirm_transfer_pair(conn, 1, 2)
        assert len(confirmed_transfer_pairs(conn)) == 1
        assert conn.execute('SELECT action FROM transfer_pair_audit WHERE pair_id=?', (pair_id,)).fetchone()[0] == 'confirm'
        after = monthly_summary(conn, '2026-11-15', '2026-11-15').iloc[0]
        assert after['income'] == 500.0
        assert after['expense'] == 10.0
        assert total_fees(conn, '2026-11-15', '2026-11-15') == 10.0
        assert len(transfer_candidates(conn, '2026-11-15', '2026-11-15')) == 0

        assert unpair_transfer(conn, pair_id)
        restored = monthly_summary(conn, '2026-11-15', '2026-11-15').iloc[0]
        assert restored['income'] == before['income']
        assert restored['expense'] == before['expense']
    finally:
        conn.close()


def test_pair_requires_confirmation_exact_currency_and_amount() -> None:
    conn = _connection()
    try:
        conn.execute('UPDATE transactions SET amount=49000 WHERE id=2')
        conn.commit()
        with pytest.raises(ValueError, match='amounts must match'):
            confirm_transfer_pair(conn, 1, 2)
        assert conn.execute('SELECT COUNT(*) FROM transfer_pairs').fetchone()[0] == 0
        conn.execute('UPDATE transactions SET amount=50000 WHERE id=2')
        conn.commit()
        confirm_transfer_pair(conn, 1, 2)
        with pytest.raises(ValueError, match='already paired'):
            confirm_transfer_pair(conn, 1, 3)
    finally:
        conn.close()


def test_full_day_date_scope_and_budget_keep_fees_separate() -> None:
    conn = _connection()
    try:
        # The outgoing item is at 23:59: it remains inside the selected end date.
        assert len(transfer_candidates(conn, '2026-11-15', '2026-11-15')) == 2
        pair_id = confirm_transfer_pair(conn, 1, 2)
        budget = budget_comparison(conn, '2026-11', currency='MNT')
        fee = budget.set_index('category').loc['Bank Fees & Charges', 'actual_minor']
        assert fee == 1000
        assert budget['actual_minor'].sum() == 1000
        assert unpair_transfer(conn, pair_id)
    finally:
        conn.close()


def test_transaction_transfer_panel_renders_empty_state(monkeypatch) -> None:
    """Keep the transaction page's transfer section wired to its core helpers."""
    from datetime import date
    import streamlit as st

    conn = get_conn(':memory:')
    init_db(conn)
    headings = []

    class Column:
        def selectbox(self, *args, **kwargs):
            return None

    monkeypatch.setattr(st, 'subheader', lambda value: headings.append(value))
    monkeypatch.setattr(st, 'caption', lambda *args, **kwargs: None)
    monkeypatch.setattr(st, 'info', lambda *args, **kwargs: None)
    monkeypatch.setattr(st, 'markdown', lambda *args, **kwargs: None)
    monkeypatch.setattr(st, 'columns', lambda count: [Column() for _ in range(count)])
    monkeypatch.setattr(st, 'button', lambda *args, **kwargs: False)
    try:
        _render_transfer_tools(conn, date(2026, 11, 1), date(2026, 11, 30), None, 'MNT')
        assert headings == ['Өөрийн дансны шилжүүлэг']
    finally:
        conn.close()

def test_transactions_page_has_shared_report_helpers():
    from ui import transactions

    assert callable(transactions.available_accounts)
    assert callable(transactions.available_currencies)
    assert callable(transactions.transaction_scope)
