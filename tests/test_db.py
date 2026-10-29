"""Verify database constraints, category seeding, and exact money handling."""

import sqlite3
from decimal import Decimal

import pytest

from core.db import (
    _PostgresConnection,
    backup_db,
    get_conn,
    init_db,
    read_sql_query,
    restore_db,
)
from core.models import to_minor


def test_init_is_idempotent() -> None:
    conn = get_conn(':memory:')
    try:
        init_db(conn)
        init_db(conn)
        counts = dict(conn.execute('SELECT type, COUNT(*) FROM categories GROUP BY type'))
        assert counts == {'income': 5, 'expense': 10, 'fee': 1, 'transfer': 1}
        assert conn.execute('SELECT SUM(is_default) FROM categories').fetchone()[0] == 17
        assert conn.execute(
            "SELECT type FROM categories WHERE name='Clothing & Footwear'"
        ).fetchone()[0] == 'expense'
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='goals'"
        ).fetchone()[0] == 'goals'
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert isinstance(conn.execute('SELECT * FROM categories').fetchone(), sqlite3.Row)
    finally:
        conn.close()


def test_dedupe_key_unique() -> None:
    conn = get_conn(':memory:')
    try:
        init_db(conn)
        conn.execute("INSERT INTO transactions(dedupe_key) VALUES ('same')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO transactions(dedupe_key) VALUES ('same')")
    finally:
        conn.close()


def test_init_backfills_import_transaction_links() -> None:
    conn = get_conn(':memory:')
    try:
        init_db(conn)
        import_id = conn.execute(
            "INSERT INTO imports(filename) VALUES ('legacy.xlsx')"
        ).lastrowid
        transaction_id = conn.execute(
            'INSERT INTO transactions(import_id) VALUES (?)',
            (import_id,),
        ).lastrowid

        init_db(conn)

        links = conn.execute(
            '''SELECT import_id, transaction_id FROM import_transactions'''
        ).fetchall()
        assert [tuple(row) for row in links] == [(import_id, transaction_id)]
    finally:
        conn.close()


def test_read_sql_query_supports_sqlite_connection() -> None:
    conn = get_conn(':memory:')
    try:
        init_db(conn)
        result = read_sql_query(
            'SELECT name, type FROM categories ORDER BY id LIMIT ?',
            conn,
            [1],
        )
        assert list(result.columns) == ['name', 'type']
        assert len(result) == 1
    finally:
        conn.close()


def test_postgres_adapter_translates_placeholders_and_returns_ids() -> None:
    query = _PostgresConnection._prepare(
        'INSERT INTO accounts(bank_code) VALUES (?)'
    )
    assert query == (
        'INSERT INTO accounts(bank_code) VALUES (%s) RETURNING id'
    )
    relation_query = _PostgresConnection._prepare(
        '''INSERT INTO import_transactions(import_id, transaction_id)
           VALUES (?, ?) ON CONFLICT(import_id, transaction_id) DO NOTHING
           RETURNING import_id'''
    )
    assert 'RETURNING id' not in relation_query
    assert 'VALUES (%s, %s)' in relation_query
    file_query = _PostgresConnection._prepare(
        '''INSERT INTO import_files(import_id, file_data, file_hash)
           VALUES (?, ?, ?) ON CONFLICT(import_id) DO UPDATE SET
           file_data=excluded.file_data RETURNING import_id'''
    )
    assert 'RETURNING id' not in file_query
    assert 'VALUES (%s, %s, %s)' in file_query


def test_postgres_connection_context_keeps_connection_open() -> None:
    class FakeConnection:
        committed = False
        rolled_back = False

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

    raw = FakeConnection()
    adapted = _PostgresConnection(raw)
    with adapted as connection:
        assert connection is adapted
    assert raw.committed
    assert not raw.rolled_back


@pytest.mark.parametrize(('value', 'expected'), [
    ('17585.28', 1758528), (-50.0, -5000),
    (Decimal('1.005'), 101), ('-1.005', -101), ('0', 0),
])
def test_to_minor(value: Decimal | str | float, expected: int) -> None:
    assert to_minor(value) == expected


def test_backup(tmp_path) -> None:
    source = tmp_path / 'finance.db'
    assert backup_db(str(source), str(tmp_path / 'backups')) is None
    conn = get_conn(str(source))
    init_db(conn)
    conn.close()
    target = backup_db(str(source), str(tmp_path / 'backups'))
    snapshot = get_conn(target)
    try:
        assert snapshot.execute('SELECT COUNT(*) FROM categories').fetchone()[0] == 17
    finally:
        snapshot.close()


def test_backup_retention(tmp_path) -> None:
    source = tmp_path / 'finance.db'
    conn = get_conn(str(source))
    init_db(conn)
    conn.close()
    folder = tmp_path / 'backups'
    folder.mkdir()
    unrelated = folder / 'notes.txt'
    unrelated.write_text('keep', encoding='utf-8')
    for _ in range(22):
        backup_db(str(source), str(folder))
    assert len(list(folder.glob('finance_*.db'))) == 20
    assert unrelated.read_text(encoding='utf-8') == 'keep'


def test_large_money_round_trips_as_integer_minor_units() -> None:
    conn = get_conn(':memory:')
    try:
        init_db(conn)
        amount_minor = 10_000_000_000  # 100,000,000.00 MNT
        conn.execute(
            "INSERT INTO transactions(amount, balance_after) VALUES (?, ?)",
            (amount_minor, amount_minor),
        )
        row = conn.execute(
            'SELECT amount, balance_after FROM transactions'
        ).fetchone()
        assert tuple(row) == (amount_minor, amount_minor)
    finally:
        conn.close()

def test_file_connections_have_isolated_transactions(tmp_path) -> None:
    path = tmp_path / 'finance.db'
    first = get_conn(str(path))
    second = get_conn(str(path))
    try:
        init_db(first)
        first.execute(
            "INSERT INTO accounts(account_number) VALUES ('first-account')"
        )
        assert second.execute(
            'SELECT COUNT(*) FROM accounts'
        ).fetchone()[0] == 0
        first.commit()
        assert second.execute(
            'SELECT COUNT(*) FROM accounts'
        ).fetchone()[0] == 1
    finally:
        first.close()
        second.close()

def test_postgres_adapter_executemany_does_not_request_ids() -> None:
    query = _PostgresConnection._prepare(
        'INSERT INTO budgets(budget_month, category_id, amount, updated_at) '
        'VALUES (?, ?, ?, ?) ON CONFLICT(budget_month, category_id) DO NOTHING',
        returning_id=False,
    )
    assert 'RETURNING' not in query
    assert 'VALUES (%s, %s, %s, %s)' in query


def test_postgres_adapter_does_not_assume_migration_table_has_id() -> None:
    query = _PostgresConnection._prepare(
        'INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)'
    )
    assert query == (
        'INSERT INTO schema_migrations(version, applied_at) VALUES (%s, %s)'
    )


def test_postgres_adapter_keeps_non_id_returning_column() -> None:
    query = _PostgresConnection._prepare(
        'INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?) '
        'RETURNING version'
    )
    assert query.endswith('RETURNING version')
    assert 'RETURNING id' not in query



def test_budget_scope_migration_preserves_existing_rows_and_is_idempotent() -> None:
    conn = get_conn(':memory:')
    try:
        init_db(conn)
        category_id = conn.execute(
            "SELECT id FROM categories WHERE name='Groceries / Food'"
        ).fetchone()[0]
        conn.execute('DROP TABLE budgets')
        conn.execute(
            '''CREATE TABLE budgets (
               budget_month TEXT NOT NULL,
               category_id INTEGER NOT NULL REFERENCES categories(id),
               amount INTEGER NOT NULL CHECK(amount >= 0),
               updated_at TEXT NOT NULL,
               PRIMARY KEY (budget_month, category_id)
            )'''
        )
        conn.execute(
            'INSERT INTO budgets VALUES (?,?,?,?)',
            ('2026-11', category_id, 123456, '2026-10-01'),
        )
        conn.execute('DELETE FROM schema_migrations WHERE version=2')
        conn.commit()

        init_db(conn)
        init_db(conn)
        row = conn.execute(
            'SELECT amount, currency, account_scope FROM budgets '
            'WHERE budget_month=? AND category_id=?',
            ('2026-11', category_id),
        ).fetchone()
        assert tuple(row) == (123456, 'MNT', 'all')
        assert conn.execute(
            'SELECT COUNT(*) FROM schema_migrations WHERE version=2'
        ).fetchone()[0] == 1
    finally:
        conn.close()



def test_classification_audit_type_migration_preserves_history() -> None:
    conn = get_conn(':memory:')
    try:
        init_db(conn)
        conn.execute('DROP TABLE classification_audit')
        conn.execute(
            '''CREATE TABLE classification_audit (
               id INTEGER PRIMARY KEY,
               entity_type TEXT NOT NULL CHECK(entity_type IN ('transaction','rule')),
               entity_id INTEGER NOT NULL,
               action TEXT NOT NULL,
               old_values TEXT,
               new_values TEXT,
               changed_at TEXT NOT NULL
            )'''
        )
        conn.execute(
            "INSERT INTO classification_audit VALUES (1,'transaction',7,'manual_classify','{}','{}','2026-10-28')"
        )
        conn.execute('DELETE FROM schema_migrations WHERE version=3')
        conn.commit()

        init_db(conn)
        conn.execute(
            "INSERT INTO classification_audit(entity_type,entity_id,action,changed_at) VALUES ('category',9,'archive','2026-10-28')"
        )
        conn.commit()
        assert conn.execute('SELECT COUNT(*) FROM classification_audit').fetchone()[0] == 2
        assert conn.execute(
            'SELECT COUNT(*) FROM schema_migrations WHERE version=3'
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_goal_planning_v4_migration_is_idempotent_and_preserves_existing_goal():
    conn = get_conn(':memory:')
    try:
        init_db(conn)
        goal_id = conn.execute(
            """INSERT INTO goals(name, target_amount, current_savings, other_funding,
                   loan_amount, annual_interest_rate, loan_term_months, loan_start_month,
                   target_date, created_at, updated_at)
               VALUES ('Өмнөх зорилго', 1000, 100, 0, 0, 0, 0, '2026-11',
                       '2027-11-01', '2026-10-01', '2026-10-01') RETURNING id"""
        ).fetchone()[0]
        init_db(conn)
        init_db(conn)
        assert conn.execute('SELECT name FROM goals WHERE id=?', (goal_id,)).fetchone()[0] == 'Өмнөх зорилго'
        assert conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=4').fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='recurring_payment_matches'").fetchone()[0] == 'recurring_payment_matches'
    finally:
        conn.close()


def test_ai_review_migrations_add_idempotency_and_approval_fields():
    conn = get_conn(':memory:')
    try:
        init_db(conn)
        init_db(conn)
        run_columns = {row['name'] for row in conn.execute('PRAGMA table_info(ai_analysis_runs)')}
        mapping_columns = {row['name'] for row in conn.execute('PRAGMA table_info(ai_expense_mappings)')}
        assert 'request_fingerprint' in run_columns
        assert {'approved_category_id', 'approved_at', 'applied_count'} <= mapping_columns
        assert conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version IN (5,6)').fetchone()[0] == 2
    finally:
        conn.close()


def test_restore_protects_current_database_and_reconciles_count_and_total(tmp_path):
    target = tmp_path / 'finance.db'
    conn = get_conn(str(target))
    try:
        init_db(conn)
        conn.execute('INSERT INTO transactions(amount) VALUES (?)', (12_345,))
        conn.commit()
    finally:
        conn.close()

    snapshots = tmp_path / 'backups'
    saved = backup_db(str(target), str(snapshots))
    assert saved is not None

    conn = get_conn(str(target))
    try:
        conn.execute('INSERT INTO transactions(amount) VALUES (?)', (67_890,))
        conn.commit()
    finally:
        conn.close()

    safety_snapshot = restore_db(saved, str(target), str(snapshots))
    assert safety_snapshot is not None
    restored = get_conn(str(target))
    protected = get_conn(safety_snapshot)
    try:
        restored_values = restored.execute(
            'SELECT COUNT(*), COALESCE(SUM(amount),0) FROM transactions'
        ).fetchone()
        protected_values = protected.execute(
            'SELECT COUNT(*), COALESCE(SUM(amount),0) FROM transactions'
        ).fetchone()
        assert tuple(restored_values) == (1, 12_345)
        assert tuple(protected_values) == (2, 80_235)
    finally:
        restored.close()
        protected.close()


def test_invalid_restore_does_not_replace_or_back_up_current_database(tmp_path):
    target = tmp_path / 'finance.db'
    conn = get_conn(str(target))
    try:
        init_db(conn)
        conn.execute('INSERT INTO transactions(amount) VALUES (123)')
        conn.commit()
    finally:
        conn.close()
    invalid = tmp_path / 'not-a-database.db'
    invalid.write_text('invalid', encoding='utf-8')

    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        restore_db(str(invalid), str(target), str(tmp_path / 'backups'))

    check = get_conn(str(target))
    try:
        assert check.execute('SELECT SUM(amount) FROM transactions').fetchone()[0] == 123
    finally:
        check.close()
    backup_folder = tmp_path / 'backups'
    assert not backup_folder.exists() or not list(backup_folder.glob('finance_*.db'))


def test_local_backup_is_not_reported_for_cloud_database(tmp_path, monkeypatch):
    source = tmp_path / 'finance.db'
    conn = get_conn(str(source))
    init_db(conn)
    conn.close()
    monkeypatch.setenv('DATABASE_URL', 'postgresql://synthetic.invalid/db')

    assert backup_db(str(source), str(tmp_path / 'backups')) is None
    with pytest.raises(RuntimeError, match='disabled'):
        restore_db(str(source), str(tmp_path / 'restore.db'))
    assert not (tmp_path / 'backups').exists()
