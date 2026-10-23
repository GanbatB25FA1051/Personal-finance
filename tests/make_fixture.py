"""Generate an entirely fictional Khan Bank statement."""
from pathlib import Path
from decimal import Decimal
from openpyxl import Workbook
from banks.khan_bank import HEADERS


def make_fixture(path: str | Path) -> None:
    """Write twelve transactions with chained balances and text totals."""
    wb = Workbook()
    ws = wb.active
    ws.append(['Хэвлэсэн огноо:', None, 'Депозит дансны дэлгэрэнгүй хуулга', None,
               'FAKE TEST HOLDER', None, '2026/10/01', None, '2026/10/12', None, 'MNT',
               'IBAN: MN030005005000000001'])
    ws.append(HEADERS)
    examples = [
        ('qpay fino-940482663759306, Дэвжих супермаркет', '-15200', None),
        ('qpay fino fee-424872322362495, шимтгэл', '-200', None),
        ('Ухаалаг банкны гүйлгээний хураамж', '-50', None),
        ('QPAY 243969128072217, 2851837,DODO PI', '-28215', None),
        ('qpay 414170975548256 UB PROSPERITY FOOD LLC 28500', '-285', None),
        ('Z,30022607125498,2509015047,261002225943899', '17585.28', None),
        ('Шилжүүлэгч ТЭРГЭЛ ХААНААС: 320000 ТЭРГЭЛ ЛХАГВАДОРЖ', '139800', None),
        ('A', '-7400', '112700229720'), ('Худалдан авалт', '20000', None),
        ('qpay 123456789, Qpay, Qmenu, Gochu - Их Дэлг', '-1200', None),
        ('qpay 123456789 шимтгэл', '-100', None), ('Test salary', '50000', None),
    ]
    balance = Decimal('200000')
    credit = debit = Decimal('0')
    for i, (description, value, counterparty) in enumerate(examples):
        amount = Decimal(value)
        day = 1 if i < 3 else (10 if i == 10 else i + 1)
        timestamp = f'2026-10-{day:02d} 12:00:00'
        ws.append([timestamp, '001', float(balance), float(max(amount, 0)), float(min(amount, 0)),
                   float(balance + amount), description, counterparty])
        balance += amount
        credit += max(amount, 0)
        debit += min(amount, 0)
    ws.append([])
    ws.append([])
    ws.append(['Нийт дүн:', None, None, str(credit), str(-debit)])
    wb.save(path)
