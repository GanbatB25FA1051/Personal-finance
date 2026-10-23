"""Check detection, validation and merchant normalization."""
from pathlib import Path

import pytest
from openpyxl import load_workbook
from banks.base import detect_parser
from banks.khan_bank import KhanBankParser
from tests.make_fixture import make_fixture


def test_fixture(tmp_path: Path) -> None:
    path = tmp_path / 'statement.xlsx'
    make_fixture(path)
    wb = load_workbook(path)
    parser = detect_parser(wb)
    assert isinstance(parser, KhanBankParser)
    result = parser.parse(wb)
    assert len(result.rows) == 12
    assert result.warnings == []
    assert result.account_number == 'MN030005005000000001'
    assert result.rows[0].amount == -1520000
    assert result.rows[5].amount == 1758528
    assert sum(max(r.amount, 0) for r in result.rows) == result.control_credit
    assert sum(min(r.amount, 0) for r in result.rows) == result.control_debit
    wb.active['F3'] = 0
    assert parser.parse(wb).warnings
    wb.close()


@pytest.mark.parametrize(('raw', 'expected'), [
    ('qpay fino-940482663759306, Дэвжих супермаркет', 'дэвжих супермаркет'),
    ('QPAY 243969128072217, 2851837,DODO PI', 'dodo pi'),
    ('qpay 414170975548256 UB PROSPERITY FOOD LLC 28500', 'ub prosperity food llc'),
    ('Z,30022607125498,2509015047,261002225943899', 'z-recurring'),
    ('qpay 123456789, Qpay, Qmenu, Gochu - Их Дэлг', 'gochu - их дэлг'),
])
def test_clean_merchant(raw: str, expected: str) -> None:
    assert KhanBankParser().clean_merchant(raw) == expected


@pytest.mark.parametrize(('raw', 'expected'), [
    ('qpay fino fee-424872322362495, шимтгэл', True),
    ('Ухаалаг банкны гүйлгээний хураамж', True),
    ('qpay 123456789 шимтгэл', True), ('DODO PI', False), ('A', False),
])
def test_is_fee(raw: str, expected: bool) -> None:
    assert KhanBankParser().is_fee(raw) is expected


def test_shifted_and_reordered_headers(tmp_path: Path) -> None:
    path = tmp_path / 'shifted.xlsx'
    make_fixture(path)
    wb = load_workbook(path)
    ws = wb.active
    ws.insert_rows(2)
    for row in ws.iter_rows(min_row=3):
        row[0].value, row[6].value = row[6].value, row[0].value
    parser = detect_parser(wb)
    result = parser.parse(wb)
    assert len(result.rows) == 12
    assert result.warnings == []
    assert result.rows[0].amount == -1520000
    wb.close()
