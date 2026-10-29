"""PostgreSQL schema migration tests against an isolated temporary schema."""

import os
import uuid

import pytest

from core.db import get_conn, init_db


@pytest.fixture
def postgres_test_conn(monkeypatch):
    url = os.environ.get('FINANCE_TEST_DATABASE_URL')
    if not url:
        pytest.skip('FINANCE_TEST_DATABASE_URL is not configured')
    psycopg = pytest.importorskip('psycopg')
    from psycopg import sql

    schema = 'finance_test_' + uuid.uuid4().hex
    admin = psycopg.connect(url)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    admin.commit()
    monkeypatch.setenv('DATABASE_URL', url)
    conn = get_conn()
    conn._raw.execute(
        sql.SQL('SET search_path TO {}').format(sql.Identifier(schema))
    )
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.commit()
        admin.close()


def test_bigint_migration_is_idempotent_and_preserves_data(postgres_test_conn):
    conn = postgres_test_conn
    conn.execute(
        'INSERT INTO transactions(amount, balance_after) VALUES (?, ?)',
        (12345, 99999),
    )
    conn.commit()
    money_columns = (
        ('budgets', 'amount'),
        ('goals', 'target_amount'),
        ('goals', 'current_savings'),
        ('goals', 'other_funding'),
        ('goals', 'loan_amount'),
        ('transactions', 'amount'),
        ('transactions', 'balance_after'),
        ('ai_expense_mappings', 'expense_total_minor'),
    )
    for table, column in money_columns:
        conn.execute(
            f'ALTER TABLE {table} ALTER COLUMN {column} TYPE INTEGER '
            f'USING {column}::INTEGER'
        )
    conn.execute('DELETE FROM schema_migrations WHERE version=1')
    conn.commit()

    init_db(conn)
    init_db(conn)

    values = conn.execute(
        'SELECT amount, balance_after FROM transactions'
    ).fetchone()
    assert tuple(values) == (12345, 99999)
    for table, column in money_columns:
        row = conn.execute(
            '''SELECT data_type FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name=?
                 AND column_name=?''',
            (table, column),
        ).fetchone()
        assert row['data_type'] == 'bigint'

    large_amount = 10_000_000_000
    conn.execute(
        'INSERT INTO transactions(amount, balance_after) VALUES (?, ?)',
        (large_amount, large_amount),
    )
    conn.commit()
    assert conn.execute(
        'SELECT amount FROM transactions WHERE amount=?', (large_amount,)
    ).fetchone()['amount'] == large_amount
