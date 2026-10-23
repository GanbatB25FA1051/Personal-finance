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
)
from core.models import to_minor


def test_init_is_idempotent() -> None:
    conn = get_conn(':memory:')
    try:
        init_db(conn)
        init_db(conn)
        counts = dict(conn.execute('SELECT type, COUNT(*) FROM categories GROUP BY type'))
        assert counts == {'income': 5, 'expense': 9, 'fee': 1, 'transfer': 1}
        assert conn.execute('SELECT SUM(is_default) FROM categories').fetchone()[0] == 16
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
        assert snapshot.execute('SELECT COUNT(*) FROM categories').fetchone()[0] == 16
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
