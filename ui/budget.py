"""Monthly budget planning and category performance."""

import sqlite3
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st

from core.budget import (
    budget_comparison,
    get_budget_categories,
    next_month_start,
    save_budgets,
)
from core.models import to_minor
from ui import category_label


def _format_minor(amount: int) -> str:
    """Format integer minor units as Mongolian tugrik."""
    units, cents = divmod(abs(int(amount)), 100)
    sign = '-' if amount < 0 else ''
    return f'{sign}{units:,}.{cents:02d} ₮'


def _budget_input(amount: int | None) -> str:
    """Format a saved budget as editable major units."""
    if amount is None:
        return ''
    units, cents = divmod(int(amount), 100)
    return f'{units}.{cents:02d}'


def render(conn: sqlite3.Connection) -> None:
    """Set next month's category targets and compare actual spending."""
    st.title('Сарын төсөв')
    st.caption(
        'Ирэх сарын төсвийг ангилал тус бүрээр төлөвлөж, тухайн сарын бодит '
        'зарлагатай харьцуулна. Төсвийг төгрөгөөр оруулна.'
    )
    month_date = st.date_input(
        'Төсөв, гүйцэтгэл харах сар',
        value=next_month_start(),
        key='budget_month',
    )
    budget_month = month_date.strftime('%Y-%m')
    categories = get_budget_categories(conn, budget_month)

    st.subheader(f'{budget_month} сарын төсөв')
    with st.form(f'budget_form_{budget_month}'):
        values: dict[int, str] = {}
        columns = st.columns(2)
        for index, row in enumerate(categories):
            column = columns[index % len(columns)]
            values[int(row['id'])] = column.text_input(
                category_label(row['name']),
                value=_budget_input(row['amount']),
                key=f'budget_amount_{budget_month}_{row["id"]}',
                help='Төсөвгүй бол хоосон үлдээнэ. Жишээ: 350000',
            )
        submitted = st.form_submit_button('Төсөв хадгалах')

    if submitted:
        amounts: dict[int, int | None] = {}
        errors: list[str] = []
        labels = {int(row['id']): category_label(row['name']) for row in categories}
        for category_id, value in values.items():
            cleaned = value.strip()
            if not cleaned:
                amounts[category_id] = None
                continue
            try:
                parsed = Decimal(cleaned)
                amount = to_minor(parsed)
            except (InvalidOperation, ValueError, TypeError):
                errors.append(f'{labels[category_id]}: мөнгөн дүн буруу байна.')
                continue
            if parsed < 0:
                errors.append(f'{labels[category_id]}: сөрөг төсөв оруулж болохгүй.')
            else:
                amounts[category_id] = amount
        if errors:
            for error in errors:
                st.error(error)
        else:
            save_budgets(conn, budget_month, amounts)
            st.success('Төсөв хадгалагдлаа.')
            st.rerun()

    comparison = budget_comparison(conn, budget_month)
    total_budget = int(comparison['budget_minor'].sum())
    total_actual = int(comparison['actual_minor'].sum())
    remaining = total_budget - total_actual
    metrics = st.columns(3)
    metrics[0].metric('Төлөвлөсөн төсөв', _format_minor(total_budget))
    metrics[1].metric('Бодит зарлага', _format_minor(total_actual))
    metrics[2].metric('Үлдэгдэл', _format_minor(remaining))
    if total_budget:
        st.caption(
            f'Нийт төсвийн гүйцэтгэл: {total_actual / total_budget * 100:,.1f}%. '
            '100%-иас их бол төсвөөс хэтэрсэн байна.'
        )
    else:
        st.info('Төсвөө ангилал тус бүрээр оруулж хадгална уу.')

    st.subheader('Ангиллаар төсөв ба гүйцэтгэл')
    if comparison.empty:
        st.info('Зарлагын ангилал олдсонгүй.')
        return

    display = comparison.copy()
    display['category'] = display['category'].map(category_label)
    display['budget'] = display['budget_minor'].map(_format_minor)
    display['actual'] = display['actual_minor'].map(_format_minor)
    display['remaining'] = display['remaining_minor'].map(_format_minor)
    display['execution'] = display['used_percent'].map(
        lambda value: 'Төсөвгүй' if pd.isna(value) else f'{value:,.1f}%'
    )
    st.dataframe(
        display[['category', 'budget', 'actual', 'remaining', 'execution']],
        hide_index=True,
        column_config={
            'category': 'Ангилал',
            'budget': 'Төсөв',
            'actual': 'Гүйцэтгэл',
            'remaining': 'Үлдэгдэл / хэтрэлт',
            'execution': 'Төсвийн гүйцэтгэл',
        },
    )

    chart = comparison[['category', 'budget_minor', 'actual_minor']].copy()
    chart['category'] = chart['category'].map(category_label)
    chart['Төсөв (₮)'] = chart['budget_minor'] / 100
    chart['Гүйцэтгэл (₮)'] = chart['actual_minor'] / 100
    st.bar_chart(
        chart.set_index('category')[['Төсөв (₮)', 'Гүйцэтгэл (₮)']]
    )
    if (comparison['remaining_minor'] < 0).any():
        st.warning('Зарим ангиллын бодит зарлага төлөвлөсөн төсвөөс хэтэрсэн байна.')
    if (
        comparison['category'].eq('Ангилалгүй')
        & (comparison['actual_minor'] > 0)
    ).any():
        st.info(
            'Ангилалгүй зарлага төсөвтэй ангилалтай харьцуулагдахгүй. '
            'Тайланг нарийвчлахын тулд гүйлгээнүүдээ ангилна уу.'
        )
