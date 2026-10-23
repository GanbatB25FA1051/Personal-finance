"""Exercise complete imports, overlapping files, and atomic rollback."""
import pytest
from openpyxl import load_workbook
from core.db import get_conn, init_db
from core.ingest import import_statement, statement_account_number
from core.classify import set_category
from tests.make_fixture import make_fixture


def test_import_and_duplicates(tmp_path) -> None:
    path = tmp_path / 'test.xlsx'
    make_fixture(path)
    assert statement_account_number(path.read_bytes()) == 'MN030005005000000001'
    conn = get_conn(':memory:')
    init_db(conn)
    first = import_statement(conn, path, path.name)
    assert first.rows_new == 12
    archived = conn.execute(
        'SELECT file_data, file_hash FROM import_files WHERE import_id=?',
        (first.import_id,),
    ).fetchone()
    assert archived['file_data'] == path.read_bytes()
    assert archived['file_hash']
    first_links = conn.execute(
        'SELECT COUNT(*) FROM import_transactions WHERE import_id=?',
        (first.import_id,),
    ).fetchone()[0]
    assert first_links == 12
    fees = conn.execute(
        """SELECT * FROM transactions
           WHERE category_id=(SELECT id FROM categories
                              WHERE name='Bank Fees & Charges')"""
    ).fetchall()
    assert len(fees) == 3
    assert all(r['parent_txn_id'] is not None for r in fees)
    assert first.auto_classified == 9
    assert first.needs_review == 0
    second = import_statement(conn, path.read_bytes(), path.name)
    assert (second.rows_new, second.rows_duplicate) == (0, 12)
    assert 'This exact file was already imported' in second.warnings
    second_links = conn.execute(
        'SELECT COUNT(*) FROM import_transactions WHERE import_id=?',
        (second.import_id,),
    ).fetchone()[0]
    assert second_links == 12
    wb = load_workbook(path)
    ws = wb.active
    ws.delete_rows(13, 2)
    for i in range(9, 13):
        ws.cell(i, 1, f'2026-11-{i:02d} 12:00:00')
    wb.save(path)
    wb.close()
    overlap = import_statement(conn, path, path.name)
    assert (overlap.rows_new, overlap.rows_duplicate) == (4, 6)
    overlap_links = conn.execute(
        'SELECT COUNT(*) FROM import_transactions WHERE import_id=?',
        (overlap.import_id,),
    ).fetchone()[0]
    assert overlap_links == 10
    conn.close()


def test_new_weekly_statement_reuses_previous_manual_merchant_category(
    tmp_path,
) -> None:
    path = tmp_path / 'weekly.xlsx'
    make_fixture(path)
    conn = get_conn(':memory:')
    init_db(conn)
    first = import_statement(conn, path, path.name)
    merchant = conn.execute(
        """SELECT id, merchant_clean FROM transactions
           WHERE raw_description LIKE 'qpay fino-940482663759306%'"""
    ).fetchone()
    assert merchant is not None
    category_id = conn.execute(
        "SELECT id FROM categories WHERE name='Groceries / Food'"
    ).fetchone()[0]
    set_category(conn, [merchant['id']], category_id)

    from openpyxl import load_workbook

    workbook = load_workbook(path)
    sheet = workbook.active
    sheet.cell(3, 1, '2026-10-20 12:00:00')
    sheet.cell(3, 7, 'qpay fino-123456789012345, Дэвжих супермаркет')
    workbook.save(path)
    workbook.close()

    second = import_statement(conn, path, 'weekly.xlsx')
    assert second.rows_new == 1
    linked = conn.execute(
        '''SELECT category_id, classified_by FROM transactions
           WHERE txn_datetime='2026-10-20 12:00:00' '''
    ).fetchone()
    assert tuple(linked) == (category_id, 'rule')
    assert second.auto_classified == 1
    assert conn.execute(
        '''SELECT COUNT(*) FROM merchant_rules
           WHERE match_type='merchant_exact'
             AND pattern=? COLLATE NOCASE''',
        (merchant['merchant_clean'],),
    ).fetchone()[0] == 1
    conn.close()


def test_invalid_file() -> None:
    conn = get_conn(':memory:')
    init_db(conn)
    with pytest.raises(ValueError, match='Unrecognized'):
        import_statement(conn, b'broken', 'bad.xlsx')
    conn.close()


def test_rollback(tmp_path, monkeypatch) -> None:
    from core import classify
    path = tmp_path / 'test.xlsx'
    make_fixture(path)
    conn = get_conn(':memory:')
    init_db(conn)
    def fail(*args):
        raise RuntimeError('forced failure')
    monkeypatch.setattr(classify, 'apply_rules', fail)
    with pytest.raises(RuntimeError):
        import_statement(conn, path, path.name)
    for table in ('accounts', 'imports', 'transactions'):
        assert conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
    conn.close()
