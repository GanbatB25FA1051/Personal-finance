"""Database connections, schema initialization, and local SQLite backups."""

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
import tempfile
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
ADDITIONAL_DEFAULT_CATEGORIES = {
    'Clothing & Footwear': 'expense',
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
        insert = re.match(
            r'\s*INSERT(?:\s+OR\s+\w+)?\s+INTO\s+["`]?([\w.]+)',
            query,
            re.IGNORECASE,
        )
        table = insert.group(1).split('.')[-1].strip('"`').lower() if insert else ''
        if returning_id and insert and table != 'schema_migrations':
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

    @property
    def in_transaction(self) -> bool:
        """Return whether psycopg has an active transaction."""
        return self._raw.info.transaction_status.name != 'IDLE'

    def close(self) -> None:
        self._raw.close()

    def __enter__(self) -> '_PostgresConnection':
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        if exc_type is None:
            self._raw.commit()
        else:
            try:
                self._raw.rollback()
            except Exception:
                # Keep the operation's original exception.
                pass
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


def _migrate_postgres_money_to_bigint(conn: Any) -> None:
    """Apply the idempotent PostgreSQL money-width migration."""
    version = 1
    if conn.execute(
        'SELECT 1 FROM schema_migrations WHERE version=?', (version,)
    ).fetchone():
        return
    with conn:
        for table, column in (
            ('budgets', 'amount'),
            ('goals', 'target_amount'),
            ('goals', 'current_savings'),
            ('goals', 'other_funding'),
            ('goals', 'loan_amount'),
            ('transactions', 'amount'),
            ('transactions', 'balance_after'),
            ('ai_expense_mappings', 'expense_total_minor'),
        ):
            conn.execute(
                f'ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT '
                f'USING {column}::BIGINT'
            )
        conn.execute(
            'INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?) RETURNING version',
            (version, datetime.now().isoformat(timespec='seconds')),
        )


def _migrate_budget_scope(conn: Any) -> None:
    """Version the budget key by currency/account while preserving old budgets."""
    version = 2
    if conn.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
        return
    owns_transaction = not bool(getattr(conn, 'in_transaction', False))
    if owns_transaction:
        conn.execute('BEGIN')
    conn.execute('SAVEPOINT budget_scope_migration')
    try:
        if is_postgres(conn):
            columns = {
                row['column_name']
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='budgets'"
                ).fetchall()
            }
            if 'currency' not in columns:
                conn.execute("ALTER TABLE budgets ADD COLUMN currency TEXT NOT NULL DEFAULT 'MNT'")
            if 'account_scope' not in columns:
                conn.execute("ALTER TABLE budgets ADD COLUMN account_scope TEXT NOT NULL DEFAULT 'all'")
            conn.execute('ALTER TABLE budgets DROP CONSTRAINT IF EXISTS budgets_pkey')
            conn.execute(
                'ALTER TABLE budgets ADD CONSTRAINT budgets_pkey '
                'PRIMARY KEY (budget_month, category_id, currency, account_scope)'
            )
        else:
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(budgets)')}
            if not {'currency', 'account_scope'}.issubset(columns):
                currency_expr = 'currency' if 'currency' in columns else "'MNT'"
                account_expr = 'account_scope' if 'account_scope' in columns else "'all'"
                conn.execute(
                    '''CREATE TABLE budgets_scoped (
                       budget_month TEXT NOT NULL,
                       category_id INTEGER NOT NULL REFERENCES categories(id),
                       amount INTEGER NOT NULL CHECK(amount >= 0),
                       updated_at TEXT NOT NULL,
                       currency TEXT NOT NULL,
                       account_scope TEXT NOT NULL,
                       PRIMARY KEY (budget_month, category_id, currency, account_scope)
                    )'''
                )
                conn.execute(
                    f'''INSERT INTO budgets_scoped
                       (budget_month, category_id, amount, updated_at, currency, account_scope)
                       SELECT budget_month, category_id, amount, updated_at,
                              {currency_expr}, {account_expr} FROM budgets'''
                )
                conn.execute('DROP TABLE budgets')
                conn.execute('ALTER TABLE budgets_scoped RENAME TO budgets')
        conn.execute(
            'INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)',
            (version, datetime.now().isoformat(timespec='seconds')),
        )
        conn.execute('RELEASE SAVEPOINT budget_scope_migration')
        if owns_transaction:
            conn.commit()
    except Exception as original_error:
        try:
            conn.execute('ROLLBACK TO SAVEPOINT budget_scope_migration')
            conn.execute('RELEASE SAVEPOINT budget_scope_migration')
        except Exception:
            pass
        if owns_transaction:
            try:
                conn.rollback()
            except Exception:
                pass
        raise original_error



def _migrate_classification_audit_types(conn: Any) -> None:
    """Allow category audit events on databases created before category auditing."""
    version = 3
    if conn.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
        return
    owns_transaction = not bool(getattr(conn, 'in_transaction', False))
    if owns_transaction:
        conn.execute('BEGIN')
    conn.execute('SAVEPOINT classification_audit_migration')
    try:
        if is_postgres(conn):
            conn.execute(
                'ALTER TABLE classification_audit DROP CONSTRAINT IF EXISTS '
                'classification_audit_entity_type_check'
            )
            conn.execute(
                "ALTER TABLE classification_audit ADD CONSTRAINT classification_audit_entity_type_check "
                "CHECK (entity_type IN ('transaction','rule','category'))"
            )
        else:
            schema_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='classification_audit'"
            ).fetchone()
            schema_sql = str(schema_row['sql'] or '') if schema_row else ''
            if "'category'" not in schema_sql:
                conn.execute(
                    '''CREATE TABLE classification_audit_new (
                       id INTEGER PRIMARY KEY,
                       entity_type TEXT NOT NULL CHECK(entity_type IN ('transaction','rule','category')),
                       entity_id INTEGER NOT NULL,
                       action TEXT NOT NULL,
                       old_values TEXT,
                       new_values TEXT,
                       changed_at TEXT NOT NULL
                    )'''
                )
                conn.execute(
                    '''INSERT INTO classification_audit_new
                       (id, entity_type, entity_id, action, old_values, new_values, changed_at)
                       SELECT id, entity_type, entity_id, action, old_values, new_values, changed_at
                       FROM classification_audit'''
                )
                conn.execute('DROP TABLE classification_audit')
                conn.execute('ALTER TABLE classification_audit_new RENAME TO classification_audit')
        conn.execute(
            'INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)',
            (version, datetime.now().isoformat(timespec='seconds')),
        )
        conn.execute('RELEASE SAVEPOINT classification_audit_migration')
        if owns_transaction:
            conn.commit()
    except Exception as original_error:
        try:
            conn.execute('ROLLBACK TO SAVEPOINT classification_audit_migration')
            conn.execute('RELEASE SAVEPOINT classification_audit_migration')
        except Exception:
            pass
        if owns_transaction:
            try:
                conn.rollback()
            except Exception:
                pass
        raise original_error



def _migrate_goal_and_planning_v4(conn: Any) -> None:
    """Add goal movement history and recurring planned commitments atomically."""
    version = 4
    if conn.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
        return
    owns_transaction = not bool(getattr(conn, 'in_transaction', False))
    if owns_transaction:
        conn.execute('BEGIN')
    conn.execute('SAVEPOINT goal_planning_migration')
    try:
        money_type = 'BIGINT' if is_postgres(conn) else 'INTEGER'
        id_primary = ('INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY'
                      if is_postgres(conn) else 'INTEGER PRIMARY KEY')
        conn.execute(f'''CREATE TABLE IF NOT EXISTS goal_savings_movements (
            id {id_primary},
            goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            movement_date TEXT NOT NULL,
            amount_minor {money_type} NOT NULL CHECK(amount_minor != 0),
            note TEXT NOT NULL DEFAULT '',
            source_transaction_id INTEGER UNIQUE REFERENCES transactions(id),
            created_at TEXT NOT NULL
        )''')
        conn.execute(f'''CREATE TABLE IF NOT EXISTS recurring_payments (
            id {id_primary},
            name TEXT NOT NULL,
            amount_minor {money_type} NOT NULL CHECK(amount_minor > 0),
            currency TEXT NOT NULL,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            category_id INTEGER REFERENCES categories(id),
            due_day INTEGER NOT NULL CHECK(due_day BETWEEN 1 AND 31),
            start_month TEXT NOT NULL,
            end_month TEXT,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS recurring_payment_matches (
            payment_id INTEGER NOT NULL REFERENCES recurring_payments(id) ON DELETE CASCADE,
            occurrence_month TEXT NOT NULL,
            transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
            confirmed_at TEXT NOT NULL,
            PRIMARY KEY(payment_id, occurrence_month)
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_goal_savings_goal_date ON goal_savings_movements(goal_id, movement_date, id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_recurring_payments_scope ON recurring_payments(currency, account_id, is_active)')
        conn.execute('INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)', (version, datetime.now().isoformat(timespec='seconds')))
        conn.execute('RELEASE SAVEPOINT goal_planning_migration')
        if owns_transaction:
            conn.commit()
    except Exception as original_error:
        try:
            conn.execute('ROLLBACK TO SAVEPOINT goal_planning_migration')
            conn.execute('RELEASE SAVEPOINT goal_planning_migration')
        except Exception:
            pass
        if owns_transaction:
            try:
                conn.rollback()
            except Exception:
                pass
        raise original_error



def _migrate_ai_request_fingerprint_v5(conn: Any) -> None:
    """Add a local idempotency key for saved AI suggestion runs."""
    version = 5
    if conn.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
        return
    owns_transaction = not bool(getattr(conn, 'in_transaction', False))
    if owns_transaction:
        conn.execute('BEGIN')
    conn.execute('SAVEPOINT ai_idempotency_migration')
    try:
        if is_postgres(conn):
            columns = {
                row['column_name'] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='ai_analysis_runs'"
                ).fetchall()
            }
        else:
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(ai_analysis_runs)')}
        if 'request_fingerprint' not in columns:
            conn.execute('ALTER TABLE ai_analysis_runs ADD COLUMN request_fingerprint TEXT')
        conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_analysis_runs_fingerprint '
            'ON ai_analysis_runs(request_fingerprint) WHERE request_fingerprint IS NOT NULL'
        )
        conn.execute(
            'INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)',
            (version, datetime.now().isoformat(timespec='seconds')),
        )
        conn.execute('RELEASE SAVEPOINT ai_idempotency_migration')
        if owns_transaction:
            conn.commit()
    except Exception as original_error:
        try:
            conn.execute('ROLLBACK TO SAVEPOINT ai_idempotency_migration')
            conn.execute('RELEASE SAVEPOINT ai_idempotency_migration')
        except Exception:
            pass
        if owns_transaction:
            try:
                conn.rollback()
            except Exception:
                pass
        raise original_error



def _migrate_ai_review_fields_v6(conn: Any) -> None:
    """Track explicit per-run approval separately from model suggestions."""
    version = 6
    if conn.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
        return
    owns_transaction = not bool(getattr(conn, 'in_transaction', False))
    if owns_transaction:
        conn.execute('BEGIN')
    conn.execute('SAVEPOINT ai_review_fields_migration')
    try:
        if is_postgres(conn):
            columns = {
                row['column_name'] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='ai_expense_mappings'"
                ).fetchall()
            }
        else:
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(ai_expense_mappings)')}
        if 'approved_category_id' not in columns:
            conn.execute('ALTER TABLE ai_expense_mappings ADD COLUMN approved_category_id INTEGER REFERENCES categories(id)')
        if 'approved_at' not in columns:
            conn.execute('ALTER TABLE ai_expense_mappings ADD COLUMN approved_at TEXT')
        if 'applied_count' not in columns:
            conn.execute('ALTER TABLE ai_expense_mappings ADD COLUMN applied_count INTEGER NOT NULL DEFAULT 0')
        conn.execute(
            'INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)',
            (version, datetime.now().isoformat(timespec='seconds')),
        )
        conn.execute('RELEASE SAVEPOINT ai_review_fields_migration')
        if owns_transaction:
            conn.commit()
    except Exception as original_error:
        try:
            conn.execute('ROLLBACK TO SAVEPOINT ai_review_fields_migration')
            conn.execute('RELEASE SAVEPOINT ai_review_fields_migration')
        except Exception:
            pass
        if owns_transaction:
            try:
                conn.rollback()
            except Exception:
                pass
        raise original_error


def init_db(conn: Any) -> None:
    """Create the schema and seed categories when the category table is empty."""
    schema = """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        );
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
        CREATE TABLE IF NOT EXISTS import_extensions (
            parent_import_id INTEGER NOT NULL REFERENCES imports(id),
            child_import_id INTEGER NOT NULL UNIQUE REFERENCES imports(id),
            PRIMARY KEY (parent_import_id, child_import_id),
            CHECK (parent_import_id != child_import_id)
        );
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE,
            type TEXT CHECK(type IN ('income','expense','transfer','fee')),
            is_default INTEGER, is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS budgets (
            budget_month TEXT NOT NULL,
            category_id INTEGER NOT NULL REFERENCES categories(id),
            amount INTEGER NOT NULL CHECK(amount >= 0),
            updated_at TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'MNT',
            account_scope TEXT NOT NULL DEFAULT 'all',
            PRIMARY KEY (budget_month, category_id, currency, account_scope)
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            target_amount INTEGER NOT NULL CHECK(target_amount > 0),
            current_savings INTEGER NOT NULL DEFAULT 0
                CHECK(current_savings >= 0),
            other_funding INTEGER NOT NULL DEFAULT 0 CHECK(other_funding >= 0),
            loan_amount INTEGER NOT NULL DEFAULT 0 CHECK(loan_amount >= 0),
            annual_interest_rate NUMERIC NOT NULL DEFAULT 0
                CHECK(annual_interest_rate >= 0),
            loan_term_months INTEGER NOT NULL DEFAULT 0
                CHECK(loan_term_months >= 0),
            loan_start_month TEXT NOT NULL,
            target_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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
        CREATE TABLE IF NOT EXISTS transfer_pairs (
            id INTEGER PRIMARY KEY,
            outgoing_transaction_id INTEGER NOT NULL UNIQUE
                REFERENCES transactions(id) ON DELETE CASCADE,
            incoming_transaction_id INTEGER NOT NULL UNIQUE
                REFERENCES transactions(id) ON DELETE CASCADE,
            confirmed_at TEXT NOT NULL,
            CHECK (outgoing_transaction_id != incoming_transaction_id)
        );
        CREATE TABLE IF NOT EXISTS transfer_pair_audit (
            id INTEGER PRIMARY KEY,
            pair_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            old_values TEXT,
            new_values TEXT,
            changed_at TEXT NOT NULL
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
            request_fingerprint TEXT,
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
            expense_total_minor INTEGER NOT NULL,
            approved_category_id INTEGER REFERENCES categories(id),
            approved_at TEXT,
            applied_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS classification_audit (
            id INTEGER PRIMARY KEY,
            entity_type TEXT NOT NULL CHECK(entity_type IN ('transaction','rule','category')),
            entity_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            old_values TEXT,
            new_values TEXT,
            changed_at TEXT NOT NULL
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
        for table, column in (
            ('budgets', 'amount'),
            ('goals', 'target_amount'),
            ('goals', 'current_savings'),
            ('goals', 'other_funding'),
            ('goals', 'loan_amount'),
            ('transactions', 'amount'),
            ('transactions', 'balance_after'),
            ('ai_expense_mappings', 'expense_total_minor'),
        ):
            schema = re.sub(
                rf'(\b{column}\s+)INTEGER(\b)',
                r'\1BIGINT\2',
                schema,
                count=1,
            )
    conn.executescript(schema)
    if is_postgres(conn):
        _migrate_postgres_money_to_bigint(conn)
    _migrate_budget_scope(conn)
    _migrate_classification_audit_types(conn)
    _migrate_goal_and_planning_v4(conn)
    _migrate_ai_request_fingerprint_v5(conn)
    _migrate_ai_review_fields_v6(conn)
    with conn:
        conn.execute(
            '''INSERT INTO import_transactions(import_id, transaction_id)
               SELECT import_id, id FROM transactions WHERE import_id IS NOT NULL
               ON CONFLICT(import_id, transaction_id) DO NOTHING
               RETURNING import_id'''
        )
        default_categories = [
            (name, kind)
            for kind, names in DEFAULT_CATEGORIES.items()
            for name in names
        ]
        default_categories.extend(ADDITIONAL_DEFAULT_CATEGORIES.items())
        conn.executemany(
            '''INSERT INTO categories(name, type, is_default) VALUES (?, ?, 1)
               ON CONFLICT(name) DO NOTHING''',
            default_categories,
        )


def _check_sqlite_file(path: Path) -> None:
    """Reject incomplete or non-app SQLite files before they are installed."""
    if not path.is_file():
        raise ValueError('SQLite database file does not exist.')
    uri = path.resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    try:
        result = conn.execute('PRAGMA integrity_check').fetchone()
        if not result or result[0] != 'ok':
            raise ValueError('SQLite database integrity check failed.')
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {'transactions', 'imports', 'categories'} <= tables:
            raise ValueError('SQLite file is not a My Finance database.')
    finally:
        conn.close()


def backup_db(path: str = 'finance.db', folder: str = 'backups') -> str | None:
    """Create and verify a consistent SQLite snapshot; cloud databases have no local backup."""
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
    fd, temporary_name = tempfile.mkstemp(
        prefix='.finance-backup-', suffix='.tmp', dir=str(destination)
    )
    os.close(fd)
    temporary = Path(temporary_name)
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(temporary))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    try:
        _check_sqlite_file(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    snapshots = sorted(destination.glob('finance_*.db'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for old in snapshots[20:]:
        if old.is_file() and old.resolve().parent == destination.resolve():
            old.unlink()
    return str(target)


def restore_db(
    backup_path: str,
    path: str = 'finance.db',
    folder: str | None = None,
) -> str | None:
    """Restore a verified SQLite snapshot after protecting the current database.

    Returns the safety snapshot path, or ``None`` when the target did not exist.
    The application must be stopped so no connection is using the target file.
    """
    if os.environ.get('DATABASE_URL', '').strip():
        raise RuntimeError('SQLite restore is disabled when DATABASE_URL is set.')
    source = Path(backup_path).resolve()
    target = Path(path).resolve()
    if source == target:
        raise ValueError('Restore source and target must be different files.')
    _check_sqlite_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f'.{target.name}-restore-', suffix='.tmp', dir=str(target.parent)
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        source_uri = source.as_uri() + '?mode=ro'
        src = sqlite3.connect(source_uri, uri=True)
        dst = sqlite3.connect(str(temporary))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
        _check_sqlite_file(temporary)

        # Stage the selected restore file before backup retention can prune it.
        safety_snapshot = None
        if target.is_file():
            safety_folder = Path(folder) if folder else target.parent / 'backups'
            safety_snapshot = backup_db(str(target), str(safety_folder))
            if safety_snapshot is None:
                raise RuntimeError('Could not protect the current SQLite database.')
        os.replace(temporary, target)
        return safety_snapshot
    finally:
        temporary.unlink(missing_ok=True)
