"""Shared Mongolian labels for Streamlit pages."""

from __future__ import annotations

import re


CATEGORY_LABELS = {
    'Earned': 'Цалин, хөдөлмөрийн орлого',
    'Allowance / Family support': 'Тэтгэмж / гэр бүлийн дэмжлэг',
    'Scholarships / Grants': 'Тэтгэлэг / буцалтгүй тусламж',
    'Gifts / Donations': 'Бэлэг / хандив',
    'Other income': 'Бусад орлого',
    'Tuition & Academic Fees': 'Сургалтын төлбөр, хураамж',
    'Textbooks, Printing & School Supplies': 'Ном, хэвлэл, хичээлийн хэрэгсэл',
    'Cafeteria, Coffee & Snacks': 'Цайны газар, кофе, хөнгөн зууш',
    'Groceries / Food': 'Хүнс, хүнсний бүтээгдэхүүн',
    'Dormitory / Rent & Utilities': 'Дотуур байр, түрээс, хэрэглээний төлбөр',
    'Public Transport / Commuting': 'Нийтийн тээвэр, зорчих зардал',
    'Social & Entertainment': 'Нийгмийн үйл ажиллагаа, үзвэр үйлчилгээ',
    'Software & Digital Subscriptions': 'Программ хангамж, цахим захиалга',
    'Other expense': 'Бусад зарлага',
    'Clothing & Footwear': 'Хувцас, гутал',
    'Bank Fees & Charges': 'Банкны шимтгэл, хураамж',
    'Transfers (own accounts)': 'Өөрийн данс хоорондын шилжүүлэг',
}
CATEGORY_LABELS_REVERSE = {
    label: name for name, label in CATEGORY_LABELS.items()
}
CATEGORY_TYPE_LABELS = {
    'income': 'Орлого',
    'expense': 'Зарлага',
    'transfer': 'Шилжүүлэг',
    'fee': 'Шимтгэл',
}
DIRECTION_LABELS = {
    'any': 'Бүгд',
    'in': 'Орлого',
    'out': 'Зарлага',
    'All': 'Бүгд',
    'In': 'Орлого',
    'Out': 'Зарлага',
}
MATCH_TYPE_LABELS = {
    'merchant_exact': 'Худалдаа, үйлчилгээний нэртэй яг тааруулах',
    'counterparty_account': 'Харьцсан дансны дугаартай тааруулах',
    'contains': 'Гүйлгээний утгад үг хайх',
    'regex': 'Тогтмол илэрхийллээр хайх',
}


def category_label(name: str) -> str:
    """Return a Mongolian label for built-in categories."""
    return CATEGORY_LABELS.get(name, name)


def category_name(label: str) -> str:
    """Return the stored category name for a displayed label."""
    return CATEGORY_LABELS_REVERSE.get(label, label)


def category_type_label(category_type: str) -> str:
    """Return a Mongolian label for a category type."""
    return CATEGORY_TYPE_LABELS.get(category_type, category_type)


def translate_import_message(message: str) -> str:
    """Translate known parser and import errors for display."""
    exact_messages = {
        'Unrecognized file/bank format: please select a valid .xlsx statement':
            'Файлын формат танигдсангүй. Зөв .xlsx хуулга сонгоно уу.',
        'Unrecognized file/bank format':
            'Файлын формат танигдсангүй. Дэмжигдсэн банкны .xlsx хуулга сонгоно уу.',
        'Statement has no account number or readable transaction rows':
            'Дансны дугаар эсвэл унших боломжтой гүйлгээ хуулгад алга.',
        'Invalid period_start metadata': 'Хуулгын эхлэх огноо буруу байна.',
        'Invalid period_end metadata': 'Хуулгын дуусах огноо буруу байна.',
        'Missing required statement columns':
            'Хуулгын шаардлагатай баганууд дутуу байна.',
        'Statement header not found': 'Хуулгын баганын гарчиг олдсонгүй.',
        'This exact file was already imported':
            'Энэ файлыг өмнө нь оруулсан байна.',
        'Possible gap in statement history':
            'Хуулгын түүхэнд тасалдал байж болзошгүй.',
        'Total credit differs from statement control total':
            'Нийт орлогын дүн хуулгын хяналтын дүнтэй зөрж байна.',
        'Total debit differs from statement control total':
            'Нийт зарлагын дүн хуулгын хяналтын дүнтэй зөрж байна.',
    }
    if message in exact_messages:
        return exact_messages[message]
    row_balance = re.fullmatch(
        r'Row (\d+): opening balance plus amount differs from closing balance',
        message,
    )
    if row_balance:
        return (
            f'{row_balance.group(1)}-р мөр: эхний үлдэгдэл дээр гүйлгээний '
            'дүнг нэмэхэд эцсийн үлдэгдэлтэй таарахгүй байна.'
        )
    row_invalid = re.fullmatch(r'Row (\d+): invalid statement data \(.*\)', message)
    if row_invalid:
        return f'{row_invalid.group(1)}-р мөрийн хуулгын мэдээлэл буруу байна.'
    return 'Хуулгыг боловсруулах явцад алдаа эсвэл анхаарах зүйл гарлаа.'


def translate_rule_error(message: str) -> str:
    """Translate rule validation errors for display."""
    exact_messages = {
        'Invalid match type': 'Тохирлын төрөл буруу байна.',
        'Invalid direction filter': 'Гүйлгээний чиглэлийн шүүлтүүр буруу байна.',
        'Pattern must be text': 'Хайх утгыг текстээр оруулна уу.',
        'Category does not exist': 'Сонгосон ангилал олдсонгүй.',
    }
    if message in exact_messages:
        return exact_messages[message]
    if message.startswith('Invalid regex:'):
        return 'Тогтмол илэрхийлэл буруу байна.'
    return 'Дүрмийн мэдээллийг шалгаад дахин оролдоно уу.'
