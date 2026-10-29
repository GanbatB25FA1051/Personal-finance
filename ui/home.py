"""Compact Mongolian home dashboard for the current reporting scope."""

import sqlite3
from datetime import date
from decimal import Decimal

import streamlit as st

from core.budget import budget_comparison
from core.reports import (
    available_accounts,
    available_currencies,
    transaction_scope,
)


def _money(value: Decimal | int | float, currency: str) -> str:
    amount = Decimal(str(value))
    sign = "−" if amount < 0 else ""
    amount = abs(amount)
    formatted = f"{amount:,.2f}"
    return f"{sign}{formatted} {'₮' if currency == 'MNT' else currency}"


def _go_to(page: str) -> None:
    st.session_state['next_page'] = page
    st.rerun()


def render(conn: sqlite3.Connection) -> None:
    """Render scoped monthly cashflow and safe navigation shortcuts."""
    today = date.today()
    start_default = today.replace(day=1)
    st.title('Нүүр хуудас')
    st.caption('Сарын санхүүгийн тойм. Үзүүлэлтүүд сонгосон огноо, данс, валютаар шүүгдэнэ.')

    with st.container(border=True):
        st.subheader('Харах хүрээ')
        left, right = st.columns(2)
        start = left.date_input('Эхлэх огноо', start_default, key='home_start')
        end = right.date_input('Дуусах огноо', today, key='home_end')
        currencies = available_currencies(conn)
        preferred = 'MNT' if 'MNT' in currencies else currencies[0]
        currency = st.selectbox(
            'Валют', currencies, index=currencies.index(preferred), key='home_currency',
            format_func=lambda code: '₮ · MNT' if code == 'MNT' else code,
        )
        accounts = {'Бүх данс': None}
        accounts.update({str(row['label']): int(row['id']) for row in available_accounts(conn, currency)})
        account_label = st.selectbox('Данс', list(accounts), key='home_account')
        account_id = accounts[account_label]

    if start > end:
        st.error('Эхлэх огноо дуусах огнооноос хойш байж болохгүй.')
        return

    start_text, end_text = start.isoformat(), end.isoformat()
    clauses, params = transaction_scope(start_text, end_text, account_id, currency)
    cashflow_clauses = [
        *clauses,
        "(c.type IS NULL OR c.type!='transfer')",
        "NOT EXISTS (SELECT 1 FROM transfer_pairs tp WHERE tp.outgoing_transaction_id=t.id OR tp.incoming_transaction_id=t.id)",
    ]
    cashflow = conn.execute(
        f"""SELECT
               COALESCE(SUM(CASE WHEN t.amount>0 THEN t.amount ELSE 0 END),0) AS income_minor,
               COALESCE(SUM(CASE WHEN t.amount<0 THEN -t.amount ELSE 0 END),0) AS expense_minor
            FROM transactions t
            LEFT JOIN categories c ON c.id=t.category_id
            LEFT JOIN accounts a ON a.id=t.account_id
            WHERE {' AND '.join(cashflow_clauses)}""",
        params,
    ).fetchone()
    income_minor = int(cashflow['income_minor'])
    expense_minor = int(cashflow['expense_minor'])
    net_minor = income_minor - expense_minor

    clauses.extend([
        "t.category_id IS NULL AND t.classified_by='none'",
        "(c.type IS NULL OR c.type!='transfer')",
        "NOT EXISTS (SELECT 1 FROM transfer_pairs tp WHERE tp.outgoing_transaction_id=t.id OR tp.incoming_transaction_id=t.id)",
    ])
    unclassified_count = int(conn.execute(
        f"""SELECT COUNT(*) FROM transactions t
            LEFT JOIN categories c ON c.id=t.category_id
            LEFT JOIN accounts a ON a.id=t.account_id
            WHERE {' AND '.join(clauses)}""",
        params,
    ).fetchone()[0])

    budget_month = end.strftime('%Y-%m')
    budgets = budget_comparison(conn, budget_month, account_id, currency)
    budgeted = budgets.loc[budgets['budget_minor'] > 0] if not budgets.empty else budgets
    has_budget = not budgeted.empty
    remaining_minor = int(budgeted['remaining_minor'].sum()) if has_budget else None

    first, second = st.columns(2)
    first.metric(f"Орлого · {currency}", _money(Decimal(income_minor) / 100, currency))
    second.metric(f"Зарлага · {currency}", _money(Decimal(expense_minor) / 100, currency))
    first, second = st.columns(2)
    first.metric(f"Цэвэр мөнгөн урсгал · {currency}", _money(Decimal(net_minor) / 100, currency))
    second.metric(
        f"Төсвийн үлдэгдэл · {budget_month}",
        _money(Decimal(remaining_minor) / 100, currency) if remaining_minor is not None else '—',
        help='Сонгосон огнооны эцсийн сарын төсвийн үлдэгдэл.',
    )
    st.metric('Ангилаагүй гүйлгээ', f'{unclassified_count:,}')
    st.caption('Цэвэр мөнгөн урсгал = орлого − зарлага. Энэ нь дансны бодит үлдэгдэл биш. Баталгаажсан шилжүүлгийг орлого, зарлагаас хасаж, шимтгэлийг зарлагад хэвээр тооцно.')

    if unclassified_count:
        st.info(f'{unclassified_count:,} гүйлгээг шалгаж ангилна уу.')
    elif income_minor == 0 and expense_minor == 0:
        st.info('Энэ хугацаанд гүйлгээ алга. Эхний банкны хуулгаа оруулаад тоймоо эхлүүлээрэй.')
    else:
        st.success('Одоогоор ангилах шаардлагатай гүйлгээ алга.')

    st.subheader('Шуурхай үйлдэл')
    if st.button('Банкны хуулга оруулах', key='home_go_upload', use_container_width=True):
        _go_to('Upload')
    if st.button(f'Гүйлгээ ангилах · {unclassified_count:,}', key='home_go_review', use_container_width=True):
        _go_to('Review')
    if st.button('Тайлан харах', key='home_go_reports', use_container_width=True):
        _go_to('Reports')
