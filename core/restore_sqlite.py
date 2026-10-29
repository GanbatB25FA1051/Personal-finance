"""Interactive command-line restore for a local SQLite finance database."""

import argparse

from core.db import restore_db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, help='Verified SQLite backup file')
    parser.add_argument('--database', default='finance.db', help='SQLite database to replace')
    parser.add_argument('--backup-folder', help='Folder for the pre-restore safety snapshot')
    args = parser.parse_args()
    print('Stop My Finance before restoring. The current database will be backed up first.')
    if input('Type RESTORE to continue: ').strip() != 'RESTORE':
        print('Restore cancelled; the database was not changed.')
        return 1
    safety = restore_db(args.backup, args.database, args.backup_folder)
    print('Restore completed and the database passed integrity validation.')
    if safety:
        print(f'Previous database safety snapshot: {safety}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
