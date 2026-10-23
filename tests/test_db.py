"""Verify database constraints, category seeding, and exact money handling."""

import sqlite3
from decimal import Decimal

import pytest

from core.db import backup_db, get_conn, init_db
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
