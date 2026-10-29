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
    previous_budget_month,
    preview_budget_copy,
    copy_budget_month,
)
from core.goals import goal_monthly_plan
from core.planning import (
    save_recurring_payment, list_recurring_payments, planned_payments_for_month,
    available_actual_expenses, confirm_planned_payment_match,
    remove_planned_payment_match, set_recurring_payment_active,
)
from core.reports import available_accounts, available_currencies
from core.models import to_minor
from ui import category_label


def _format_minor(amount: int, currency: str = 'MNT') -> str:
    """Format integer minor units in the selected currency."""
    units, cents = divmod(abs(int(amount)), 100)
    sign = '-' if amount < 0 else ''
    symbol = '₮' if currency.upper() == 'MNT' else currency.upper()
    return f'{sign}{units:,}.{cents:02d} {symbol}'


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
        'зарлагатай харьцуулна. Валют, дансны scope бүр тусдаа төсөвтэй.'
    )
    month_date = st.date_input(
        'Төсөв, гүйцэтгэл харах сар',
        value=next_month_start(),
        key='budget_month',
    )
    budget_month = month_date.strftime('%Y-%m')
    currencies = available_currencies(conn)
    default_currency = 'MNT' if 'MNT' in currencies else currencies[0]
    currency = st.selectbox(
        'Төсвийн валют', currencies, index=currencies.index(default_currency),
        key='budget_currency',
    )
    account_options = {'Бүх данс': None}
    account_options.update({
        str(row['label']): int(row['id'])
        for row in available_accounts(conn, currency)
    })
    account_label = st.selectbox('Данс', list(account_options), key='budget_account')
    account_id = account_options[account_label]
    scope_key = f'{budget_month}_{currency}_{account_id or "all"}'
    categories = get_budget_categories(conn, budget_month, currency, account_id)

    st.subheader(f'{budget_month} сарын төсөв · {account_label} · {currency}')
    source_month = previous_budget_month(budget_month)
    copy_key = f'{budget_month}_{currency}_{account_id or "all"}'
    preview_state_key = 'budget_copy_preview_' + copy_key
    if st.button(f'{source_month} сарын төсвийг урьдчилан харах', key='budget_copy_preview_button'):
        st.session_state[preview_state_key] = preview_budget_copy(
            conn, source_month, budget_month, account_id, currency,
        )
    copy_preview = st.session_state.get(preview_state_key)
    if copy_preview is not None:
        if copy_preview:
            preview_frame = pd.DataFrame(copy_preview)
            preview_frame['Төлөв'] = preview_frame['has_target'].map(
                lambda exists: 'Одоо байгаа дүнг хэвээр үлдээнэ' if exists else 'Хуулахад бэлэн'
            )
            preview_frame['Өмнөх сарын дүн'] = preview_frame['source_amount'].map(
                lambda amount: _format_minor(int(amount), currency)
            )
            preview_frame['Одоогийн дүн'] = preview_frame['target_amount'].map(
                lambda amount: '—' if pd.isna(amount) else _format_minor(int(amount), currency)
            )
            st.dataframe(
                preview_frame[['category', 'Өмнөх сарын дүн', 'Одоогийн дүн', 'Төлөв']],
                hide_index=True,
                column_config={'category': 'Ангилал'},
            )
            missing_count = sum(not bool(row['has_target']) for row in copy_preview)
            conflict_count = len(copy_preview) - missing_count
            st.caption(
                f'{missing_count} мөрийг хуулж болно. {conflict_count} одоо байгаа дүнг өөрчлөхгүй.'
            )
            if st.button(
                'Зөвхөн байхгүй төсвийг хуулах',
                key='budget_copy_apply_' + copy_key,
                disabled=missing_count == 0,
            ):
                result = copy_budget_month(conn, source_month, budget_month, account_id, currency)
                st.session_state.pop(preview_state_key, None)
                for row in categories:
                    st.session_state.pop(f'budget_amount_{scope_key}_{row["id"]}', None)
                st.success(f"{result['copied']} мөр хууллаа; {result['skipped_conflicts']} одоо байгаа мөрийг хэвээр үлдээлээ.")
                st.rerun()
        else:
            st.info(f'{source_month} сард хуулах төсөв алга.')
    with st.form(f'budget_form_{scope_key}'):
        values: dict[int, str] = {}
        columns = st.columns(2)
        for index, row in enumerate(categories):
            column = columns[index % len(columns)]
            values[int(row['id'])] = column.text_input(
                category_label(row['name']),
                value=_budget_input(row['amount']),
                key=f'budget_amount_{scope_key}_{row["id"]}',
                help=f'Төсөвгүй бол хоосон үлдээнэ. Жишээ: 350000 {currency}',
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
            save_budgets(conn, budget_month, amounts, account_id, currency)
            st.success('Төсөв хадгалагдлаа.')
            st.rerun()

    st.subheader('Давтагдах төлбөрийн төлөвлөгөө')
    st.caption('Төлөвлөгөө actual гүйлгээ үүсгэхгүй. Бодит гүйлгээтэй холбохыг та өөрөө баталгаажуулна.')
    expense_categories = [row for row in categories if row['type'] in ('expense', 'fee')]
    with st.expander('Сарын төлбөрийн төлөвлөгөө нэмэх', expanded=False):
        with st.form(f'recurring_payment_create_{scope_key}'):
            payment_name = st.text_input('Төлбөрийн нэр', key=f'recurring_payment_name_{scope_key}')
            left, right = st.columns(2)
            payment_amount = left.text_input(f'Сарын дүн ({currency})', key=f'recurring_payment_amount_{scope_key}')
            due_day = right.number_input('Төлөх өдөр', min_value=1, max_value=31, value=1, key=f'recurring_payment_due_day_{scope_key}')
            payment_category_label = st.selectbox(
                'Ангилал', ['Ангилалгүй', *[category_label(row['name']) for row in expense_categories]],
                key=f'recurring_payment_category_{scope_key}',
            )
            payment_start = st.date_input('Эхлэх сар', value=month_date.replace(day=1), key=f'recurring_payment_start_{scope_key}')
            payment_ends = st.checkbox('Дуусах сар тохируулах', key=f'recurring_payment_has_end_{scope_key}')
            payment_end = st.date_input('Дуусах сар', value=month_date.replace(day=1), key=f'recurring_payment_end_{scope_key}') if payment_ends else None
            payment_submitted = st.form_submit_button('Төлөвлөгөө хадгалах')
        if payment_submitted:
            try:
                amount_minor = to_minor(Decimal(payment_amount.strip()))
                selected_category_id = next(
                    (int(row['id']) for row in expense_categories
                     if category_label(row['name']) == payment_category_label),
                    None,
                )
                save_recurring_payment(
                    conn, payment_name, amount_minor, currency, int(due_day),
                    payment_start.strftime('%Y-%m'),
                    payment_end.strftime('%Y-%m') if payment_end else None,
                    account_id, selected_category_id,
                )
                st.success('Төлөвлөгөө хадгалагдлаа. Actual гүйлгээ үүсгээгүй.')
                st.rerun()
            except (ValueError, InvalidOperation) as exc:
                st.error(str(exc))

    occurrences = planned_payments_for_month(conn, budget_month, account_id, currency)
    for item in occurrences:
        with st.container(border=True):
            st.write(f"{item['name']} · {item['due_date']} · {_format_minor(item['amount_minor'], currency)}")
            if item['is_matched']:
                st.success(
                    f"Баталгаажуулсан бодит гүйлгээтэй холбосон: {_format_minor(abs(item['matched_amount_minor']), currency)} · {str(item['matched_date'])[:10]}"
                )
                if st.button('Холбоос салгах', key=f"unmatch_recurring_{item['id']}_{budget_month}"):
                    remove_planned_payment_match(conn, item['id'], budget_month)
                    st.rerun()
            else:
                candidates = available_actual_expenses(
                    conn, budget_month,
                    item['account_id'] if item['account_id'] is not None else account_id,
                    currency,
                )
                if candidates:
                    labels = {
                        f"{str(row['txn_datetime'])[:10]} · {_format_minor(abs(row['amount']), currency)} · {str(row['raw_description'] or row['merchant_clean'] or 'утгагүй')[:50]} · #{row['id']}": int(row['id'])
                        for row in candidates
                    }
                    chosen_actual = st.selectbox(
                        'Бодит гүйлгээ сонгоно уу', list(labels),
                        key=f"recurring_actual_{item['id']}_{budget_month}",
                    )
                    if st.button('Сонгосон гүйлгээг холбох', key=f"match_recurring_{item['id']}_{budget_month}"):
                        try:
                            confirm_planned_payment_match(
                                conn, item['id'], budget_month, labels[chosen_actual],
                            )
                            st.success('Холбоос баталгаажлаа; төлөвлөгөөг actual дүнд давхар тооцохгүй.')
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))
                else:
                    st.caption('Энэ сард холбож болох бодит зарлагын гүйлгээ алга.')

    active_recurring = list_recurring_payments(conn)
    if active_recurring:
        with st.expander('Давтагдах төлбөрийг түр зогсоох'):
            for payment in active_recurring:
                if payment['is_active']:
                    label = f"{payment['name']} · {_format_minor(payment['amount_minor'], payment['currency'])}"
                    if st.button('Түр зогсоох · ' + label, key=f"pause_recurring_{payment['id']}"):
                        set_recurring_payment_active(conn, payment['id'], False)
                        st.rerun()

    comparison = budget_comparison(conn, budget_month, account_id, currency)
    uncategorized_mask = comparison['category'].eq('Ангилалгүй')
    uncategorized_actual = int(comparison.loc[uncategorized_mask, 'actual_minor'].sum())
    budgeted_rows = comparison.loc[~uncategorized_mask]
    total_budget = int(budgeted_rows['budget_minor'].sum())
    categorized_actual = int(budgeted_rows['actual_minor'].sum())
    rows_with_budget = budgeted_rows.loc[budgeted_rows['budget_minor'] > 0]
    execution_actual = int(rows_with_budget['actual_minor'].sum())
    remaining = int(rows_with_budget['remaining_minor'].sum())
    goal_plan = goal_monthly_plan(conn, budget_month) if currency == 'MNT' else pd.DataFrame()
    goal_savings = int(goal_plan['monthly_saving_minor'].sum()) if not goal_plan.empty else 0
    goal_loans = int(goal_plan['monthly_loan_minor'].sum()) if not goal_plan.empty else 0
    recurring_plan = planned_payments_for_month(conn, budget_month, account_id, currency)
    recurring_unmatched = sum(int(item['unmatched_planned_minor']) for item in recurring_plan)
    metrics = st.columns(5)
    metrics[0].metric('Ангиллын төсөв', _format_minor(total_budget, currency))
    metrics[1].metric('Ангилсан actual зарлага', _format_minor(categorized_actual, currency))
    metrics[2].metric('Төсвийн үлдэгдэл', _format_minor(remaining, currency))
    metrics[3].metric('Ангилалгүй зарлага', _format_minor(uncategorized_actual, currency))
    metrics[4].metric('Бодит гүйлгээнд холбогдоогүй давтагдах төлбөр', _format_minor(recurring_unmatched, currency))
    if total_budget > 0:
        st.caption(
            f'Төсөвтэй ангиллын гүйцэтгэл: {execution_actual / total_budget * 100:,.1f}%. '
            'Ангилалгүй зарлагыг энэ хувьд оруулаагүй.'
        )
    else:
        st.info('Төсвийн дүн 0 тул гүйцэтгэлийн хувь тооцохгүй. Ангилал бүрт төсөв оруулах эсвэл ангилалгүй зарлагыг тусад нь шалгана уу.')
    st.caption('Actual = бодит ангилсан гүйлгээ; ангилалгүй зарлага тусдаа. Үлдэгдэл ба гүйцэтгэл зөвхөн төсөвтэй ангилалд тооцогдоно. Төлөвлөсөн амлалт = холбогдоогүй давтагдах төлбөр + зорилгын тооцоо. Зорилгын төлөвлөгөө actual зарлага үүсгэхгүй.')

    if not goal_plan.empty:
        st.subheader('Урт хугацааны зорилгын сарын төлөвлөгөө')
        st.caption(
            'Эдгээр нь зорилгод хуримтлуулах болон зээлийн төлөлтийн төлөвлөгөө; '
            'ангиллын бодит зарлагад давхардуулж тооцоогүй.'
        )
        goal_display = goal_plan[['name', 'monthly_saving_minor', 'monthly_loan_minor']].copy()
        goal_display['Хуримтлал'] = goal_display['monthly_saving_minor'].map(
            _format_minor
        )
        goal_display['Зээлийн төлөлт'] = goal_display['monthly_loan_minor'].map(
            _format_minor
        )
        goal_display['Нийт'] = (
            goal_display['monthly_saving_minor']
            + goal_display['monthly_loan_minor']
        ).map(_format_minor)
        st.dataframe(
            goal_display[['name', 'Хуримтлал', 'Зээлийн төлөлт', 'Нийт']],
            hide_index=True,
            column_config={
                'name': 'Зорилго',
                'Хуримтлал': 'Сарын хуримтлал',
                'Зээлийн төлөлт': 'Сарын зээлийн төлөлт',
                'Нийт': 'Сарын нийт',
            },
        )

    st.subheader('Ангиллаар төсөв ба гүйцэтгэл')
    if comparison.empty:
        st.info('Зарлагын ангилал олдсонгүй.')
        return

    display = comparison.copy()
    display['category'] = display['category'].map(category_label)
    display['budget'] = display['budget_minor'].map(lambda value: _format_minor(value, currency))
    display['actual'] = display['actual_minor'].map(lambda value: _format_minor(value, currency))
    display['remaining'] = display.apply(
        lambda row: '—' if row['category'] == 'Ангилалгүй' or row['budget_minor'] == 0 else _format_minor(row['remaining_minor'], currency), axis=1
    )
    display['execution'] = display['used_percent'].map(
        lambda value: '—' if pd.isna(value) else f'{value:,.1f}%'
    )
    st.caption('Төсөвгүй буюу 0 төгрөгийн мөрөнд гүйцэтгэлийн хувь тодорхойгүй (0%-тай адил биш). Ангилалгүй зарлагад төсвийн үлдэгдэл/хувь тооцохгүй.')
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
    currency_label = '₮' if currency == 'MNT' else currency
    budget_label = f'Төсөв ({currency_label})'
    actual_label = f'Гүйцэтгэл ({currency_label})'
    chart[budget_label] = chart['budget_minor'] / 100
    chart[actual_label] = chart['actual_minor'] / 100
    st.bar_chart(chart.set_index('category')[[budget_label, actual_label]])
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
