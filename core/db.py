"""SQLite schema, initial categories, and database backups."""

import sqlite3
from datetime import datetime
from pathlib import Path


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


def get_conn(path: str = 'finance.db') -> sqlite3.Connection:
    """Open a connection with named rows and foreign keys enabled."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema and seed categories when the category table is empty."""
    conn.executescript("""
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
    """)
    with conn:
        if conn.execute('SELECT COUNT(*) FROM categories').fetchone()[0] == 0:
            conn.executemany(
                'INSERT INTO categories(name, type, is_default) VALUES (?, ?, 1)',
                [(name, kind) for kind, names in DEFAULT_CATEGORIES.items() for name in names],
            )


def backup_db(path: str = 'finance.db', folder: str = 'backups') -> str | None:
    """Create a consistent SQLite snapshot; return None for a missing database."""
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
