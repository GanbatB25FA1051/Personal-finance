"""Copy an existing SQLite finance database into an empty Neon database."""

import argparse
import sqlite3
from pathlib import Path

from core.db import get_conn, init_db, is_postgres

TABLES = (
    'accounts',
    'categories',
    'merchant_rules',
    'imports',
    'import_files',
    'transactions',
    'import_transactions',
    'ai_analysis_runs',
    'ai_category_mappings',
    'ai_expense_mappings',
)


def migrate(source_path: str) -> dict[str, int]:
    """Copy all application rows, preserving IDs and relationships."""
    source_file = Path(source_path)
    if not source_file.is_file():
        raise FileNotFoundError(f'SQLite database not found: {source_file}')

    destination = get_conn()
    if not is_postgres(destination):
        destination.close()
        raise RuntimeError('Set DATABASE_URL to the Neon PostgreSQL connection URL.')
    source = sqlite3.connect(str(source_file))
    source.row_factory = sqlite3.Row

    try:
        init_db(destination)
        init_db(source)
        for table in TABLES:
            if table != 'categories' and destination.execute(
                f'SELECT COUNT(*) FROM {table}'
            ).fetchone()[0]:
                raise RuntimeError(
                    'The Neon database is not empty. Migration stopped without '
                    'overwriting existing records.'
                )

        source_counts: dict[str, int] = {}
        for table in TABLES:
            rows = source.execute(f'SELECT * FROM {table}').fetchall()
            source_counts[table] = len(rows)
            if not rows:
                continue
            columns = [column['name'] for column in source.execute(
                f'PRAGMA table_info({table})'
            )]
            placeholders = ', '.join('?' for _ in columns)
            names = ', '.join(columns)
            if table == 'categories':
                updates = ', '.join(
                    f'{column}=EXCLUDED.{column}'
                    for column in columns
                    if column != 'id'
                )
                conflict = f'ON CONFLICT(id) DO UPDATE SET {updates}'
            elif table == 'import_transactions':
                conflict = (
                    'ON CONFLICT(import_id, transaction_id) DO NOTHING'
                )
            elif table == 'import_files':
                conflict = 'ON CONFLICT(import_id) DO NOTHING'
            else:
                conflict = 'ON CONFLICT(id) DO NOTHING'
            statement = (
                f'INSERT INTO {table} ({names}) VALUES ({placeholders}) '
                f'{conflict}'
            )
            if table == 'import_transactions':
                statement += ' RETURNING import_id'
            elif table == 'import_files':
                statement += ' RETURNING import_id'
            for row in rows:
                destination.execute(statement, tuple(row)).fetchone()

        for table in TABLES:
            target_count = destination.execute(
                f'SELECT COUNT(*) FROM {table}'
            ).fetchone()[0]
            if target_count != source_counts[table]:
                raise RuntimeError(
                    f'Migration verification failed for table {table}: '
                    f'{source_counts[table]} source rows, {target_count} target rows.'
                )
            if table not in ('import_transactions', 'import_files'):
                destination.execute(
                    f'''SELECT setval(
                            pg_get_serial_sequence('{table}', 'id'),
                            COALESCE(MAX(id), 1),
                            MAX(id) IS NOT NULL
                        ) FROM {table}'''
                )
        destination.commit()
        return source_counts
    except Exception:
        destination.rollback()
        raise
    finally:
        source.close()
        destination.close()


def main() -> None:
    """Run the one-time SQLite-to-Neon migration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='finance.db')
    args = parser.parse_args()
    counts = migrate(args.source)
    print('Migration complete. Rows copied by table:')
    for table, count in counts.items():
        print(f'  {table}: {count}')


if __name__ == '__main__':
    main()
