"""Database connections, schema initialization, and local SQLite backups."""

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_CATEGORIES = {
    'income': ['Earned', 'Allowance / Family support', 'Scholarships / Grants',
               'Gifts / Donations', 'Other income'],
    'expense': ['Tuition & Academic Fees', 'Textbooks, Printing & School Supplies',
                'Cafeteria, Coffee & Snacks', 'Groceries / Food',
                'Dormitory / Rent & Utilities', 'Public Transport / Commuting',
                'Social & Entertainment', 'Software & Digital Subscriptions',
                'Other expense'],
    'fee': ['Bank Fees & Charges'],
    'transfer': ['Transfers (own accounts)'],
}


class _HybridRow(dict[str, Any]):
    """Support both named and positional database row access."""

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _postgres_row(cursor: Any) -> Any:
    """Build a row supporting the sqlite3.Row access pattern."""
    return lambda values: _HybridRow(
        (column.name, value)
        for column, value in zip(cursor.description, values)
    )


class _PostgresCursor:
    """Expose the sqlite cursor methods used by this application."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._first_row: Any = None
        self._first_loaded = False

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        if not self._first_loaded:
            self._first_row = self._cursor.fetchone()
            self._first_loaded = True
        return None if self._first_row is None else self._first_row['id']

    @property
    def description(self) -> Any:
        return self._cursor.description

    def fetchone(self) -> Any:
        if self._first_loaded:
            row = self._first_row
            self._first_row = None
            self._first_loaded = False
            return row
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        rows = self._cursor.fetchall()
        if self._first_row is not None:
            rows.insert(0, self._first_row)
            self._first_row = None
            self._first_loaded = False
        return rows

    def __iter__(self) -> Any:
        if self._first_row is not None:
            row = self._first_row
            self._first_row = None
            self._first_loaded = False
            yield row
        yield from self._cursor


class _PostgresConnection:
    """Adapt psycopg to the small sqlite connection API used by the app."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    @staticmethod
    def _prepare(sql: str, returning_id: bool = True) -> str:
        query = sql.replace('?', '%s')
        if re.match(r'\s*PRAGMA\b', query, re.IGNORECASE):
            return ''
        if returning_id and re.match(r'\s*INSERT\b', query, re.IGNORECASE):
            if not re.search(r'\bRETURNING\b', query, re.IGNORECASE):
                query = query.rstrip().rstrip(';') + ' RETURNING id'
        return query

    def execute(self, sql: str, parameters: Any = ()) -> _PostgresCursor:
        query = self._prepare(sql)
        if not query:
            return _PostgresCursor(self._raw.execute('SELECT 1 WHERE FALSE'))
        try:
            return _PostgresCursor(self._raw.execute(query, parameters))
        except Exception as exc:
            from psycopg import IntegrityError

            if isinstance(exc, IntegrityError):
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise

    def executemany(self, sql: str, parameters: Any) -> _PostgresCursor:
        query = self._prepare(sql, returning_id=False)
        try:
            return _PostgresCursor(
                self._raw.cursor().executemany(query, parameters)
            )
        except Exception as exc:
            from psycopg import IntegrityError

            if isinstance(exc, IntegrityError):
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise

    def executescript(self, script: str) -> None:
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    def __enter__(self) -> '_PostgresConnection':
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()
        return False


def is_postgres(conn: Any) -> bool:
    """Return whether a connection targets PostgreSQL."""
    return isinstance(conn, _PostgresConnection)


def get_conn(path: str = 'finance.db') -> Any:
    """Open PostgreSQL when configured, otherwise open the local SQLite file."""
    database_url = os.environ.get('DATABASE_URL', '').strip()
    if database_url:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                'DATABASE_URL is set but psycopg is not installed.'
            ) from exc
        raw = psycopg.connect(
            database_url,
            row_factory=lambda cursor: _postgres_row(cursor),
        )
        return _PostgresConnection(raw)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def read_sql_query(
    query: str,
    conn: Any,
    params: Any = None,
) -> pd.DataFrame:
    """Read query results into a DataFrame for SQLite or PostgreSQL."""
    cursor = conn.execute(query, () if params is None else params)
    columns = [column[0] if isinstance(column, tuple) else column.name
               for column in cursor.description or ()]
    rows = cursor.fetchall()
    records = [dict(row) if not isinstance(row, tuple) else row for row in rows]
    return pd.DataFrame.from_records(records, columns=columns)


def init_db(conn: Any) -> None:
    """Create the schema and seed categories when the category table is empty."""
    schema = """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY, bank_code TEXT, account_number TEXT UNIQUE,
            currency TEXT, nickname TEXT
        );
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id),
            filename TEXT, file_hash TEXT, period_start TEXT, period_end TEXT,
            rows_parsed INTEGER, rows_new INTEGER, rows_duplicate INTEGER,
            imported_at TEXT
        );
        CREATE TABLE IF NOT EXISTS import_files (
            import_id INTEGER PRIMARY KEY REFERENCES imports(id) ON DELETE CASCADE,
            file_data BLOB NOT NULL,
            file_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE,
            type TEXT CHECK(type IN ('income','expense','transfer','fee')),
            is_default INTEGER, is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS merchant_rules (
            id INTEGER PRIMARY KEY,
            match_type TEXT CHECK(match_type IN
                ('counterparty_account','merchant_exact','contains','regex')),
            pattern TEXT,
            direction_filter TEXT DEFAULT 'any'
                CHECK(direction_filter IN ('in','out','any')),
            category_id INTEGER REFERENCES categories(id),
            priority INTEGER DEFAULT 100, is_active INTEGER DEFAULT 1,
            hit_count INTEGER DEFAULT 0, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id),
            import_id INTEGER REFERENCES imports(id), txn_datetime TEXT,
            amount INTEGER, direction TEXT CHECK(direction IN ('in','out')),
            balance_after INTEGER, raw_description TEXT,
            counterparty_account TEXT, branch_code TEXT, merchant_clean TEXT,
            category_id INTEGER REFERENCES categories(id),
            classified_by TEXT DEFAULT 'none'
                CHECK(classified_by IN ('rule','manual','system','none')),
            rule_id INTEGER REFERENCES merchant_rules(id), note TEXT DEFAULT '',
            parent_txn_id INTEGER REFERENCES transactions(id), dedupe_key TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS import_transactions (
            import_id INTEGER NOT NULL REFERENCES imports(id),
            transaction_id INTEGER NOT NULL REFERENCES transactions(id),
            PRIMARY KEY (import_id, transaction_id)
        );
        CREATE TABLE IF NOT EXISTS ai_category_mappings (
            id INTEGER PRIMARY KEY,
            match_type TEXT NOT NULL CHECK(match_type IN ('merchant_exact','contains')),
            mapping_key TEXT NOT NULL,
            match_value TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('in','out')),
            category_id INTEGER NOT NULL REFERENCES categories(id),
            confidence REAL NOT NULL,
            reason TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            rule_id INTEGER REFERENCES merchant_rules(id) ON DELETE SET NULL,
            applied_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(match_type, mapping_key, direction)
        );
        CREATE TABLE IF NOT EXISTS ai_analysis_runs (
            id INTEGER PRIMARY KEY,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            created_at TEXT NOT NULL,
            transaction_count INTEGER NOT NULL,
            mapping_count INTEGER NOT NULL,
            auto_classified INTEGER NOT NULL DEFAULT 0,
            needs_review INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS ai_expense_mappings (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES ai_analysis_runs(id) ON DELETE CASCADE,
            match_type TEXT NOT NULL CHECK(match_type IN ('merchant_exact','contains')),
            mapping_key TEXT NOT NULL,
            match_value TEXT NOT NULL,
            category_id INTEGER NOT NULL REFERENCES categories(id),
            confidence REAL NOT NULL,
            reason TEXT NOT NULL,
            transaction_count INTEGER NOT NULL,
            expense_total_minor INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_transactions_datetime ON transactions(txn_datetime);
        CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id);
        CREATE INDEX IF NOT EXISTS idx_transactions_merchant ON transactions(merchant_clean);
        CREATE INDEX IF NOT EXISTS idx_transactions_counterparty ON transactions(counterparty_account);
        CREATE INDEX IF NOT EXISTS idx_import_transactions_transaction
            ON import_transactions(transaction_id);
    """
    if is_postgres(conn):
        schema = re.sub(
            r'\bINTEGER PRIMARY KEY\b',
            'INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY',
            schema,
        )
        schema = re.sub(r'\bBLOB\b', 'BYTEA', schema)
    conn.executescript(schema)
    with conn:
        conn.execute(
            '''INSERT INTO import_transactions(import_id, transaction_id)
               SELECT import_id, id FROM transactions WHERE import_id IS NOT NULL
               ON CONFLICT(import_id, transaction_id) DO NOTHING
               RETURNING import_id'''
        )
        if conn.execute('SELECT COUNT(*) FROM categories').fetchone()[0] == 0:
            conn.executemany(
                'INSERT INTO categories(name, type, is_default) VALUES (?, ?, 1)',
                [(name, kind) for kind, names in DEFAULT_CATEGORIES.items() for name in names],
            )


def backup_db(path: str = 'finance.db', folder: str = 'backups') -> str | None:
    """Create a consistent SQLite snapshot; return None without a local database."""
    if os.environ.get('DATABASE_URL', '').strip():
        return None
    source = Path(path)
    if not source.is_file():
        return None
    destination = Path(folder)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    target = destination / f'finance_{stamp}.db'
    if target.exists():
        target = destination / f'finance_{stamp}_{datetime.now():%f}.db'
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    snapshots = sorted(destination.glob('finance_*.db'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for old in snapshots[20:]:
        if old.is_file() and old.resolve().parent == destination.resolve():
            old.unlink()
    return str(target)
