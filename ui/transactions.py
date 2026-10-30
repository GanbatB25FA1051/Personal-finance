"""Filtered transaction browsing, manual editing and CSV export."""

import hashlib
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import streamlit as st

from core.classify import preview_rule_scope, set_category
from core.db import read_sql_query
from core.models import to_minor
from core.reports import available_accounts, available_currencies, transaction_scope
from core.transfers import (
    available_transfer_transactions,
    confirmed_transfer_pairs,
    confirm_transfer_pair,
    transfer_candidates,
    unpair_transfer,
)
from ui import DIRECTION_LABELS, category_label, category_name


def _format_minor(amount: int) -> str:
    """Format signed minor units as a two-decimal amount."""
    sign = '-' if amount < 0 else ''
    units, cents = divmod(abs(amount), 100)
    return f'{sign}{units}.{cents:02d}'



def _render_transfer_tools(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    account_id: int | None,
    currency: str,
) -> None:
    st.subheader('Өөрийн дансны шилжүүлэг')
    currency_label = '₮' if currency == 'MNT' else currency
    st.caption(
        'Огноо, дүн таарсан хосуудыг зөвхөн санал болгоно. '
        'Та шалгаж баталсны дараа л тайлангаас шилжүүлгийг хасна.'
    )
    candidates = transfer_candidates(
        conn, start.isoformat(), end.isoformat(), account_id, currency,
    )
    if not candidates:
        st.info('Баталгаажуулах шинэ санал алга.')
    else:
        for candidate in candidates:
            amount = _format_minor(candidate['amount_minor'])
            label = (
                f"{candidate['outgoing_date'][:10]} · {amount} {currency} · "
                f"{candidate['outgoing_account']} → {candidate['incoming_account']}"
            )
            with st.container(border=True):
                st.write(label)
                if candidate['counterpart_match'] is True:
                    st.caption('Харилцагч хоёр дансны дугаар харилцан таарч байна.')
                elif candidate['counterpart_match'] is False:
                    st.warning('Дансны дугаар зөрүүтэй. Батлахаас өмнө шалгана уу.')
                else:
                    st.warning('Харилцагч дансны мэдээлэл дутуу. Гүйлгээг гараар шалгана уу.')
                if st.button(
                    'Энэ хосыг батлах',
                    key=f"confirm_transfer_{candidate['outgoing_id']}_{candidate['incoming_id']}",
                ):
                    try:
                        confirm_transfer_pair(
                            conn, candidate['outgoing_id'], candidate['incoming_id']
                        )
                        st.success('Шилжүүлгийн хос баталгаажлаа.')
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
    st.markdown('**Дансны мэдээлэл дутуу үед гараар хослох**')
    unpaired = available_transfer_transactions(conn, currency=currency)
    outgoing = [row for row in unpaired if row['direction'] == 'out']
    incoming = [row for row in unpaired if row['direction'] == 'in']
    def option_label(row: dict) -> str:
        description = row['description'].strip()[:55] or 'утгагүй'
        return (
            f"#{row['id']} · {row['date'][:10]} · "
            f"{_format_minor(abs(row['amount_minor']))} {currency_label} · "
            f"{row['account']} · {description}"
        )
    out_options = {option_label(row): row for row in outgoing}
    in_options = {option_label(row): row for row in incoming}
    left, right = st.columns(2)
    out_label = left.selectbox(
        'Гарах гүйлгээ', list(out_options), key='transfer_manual_out',
        disabled=not out_options,
    )
    in_label = right.selectbox(
        'Орох гүйлгээ', list(in_options), key='transfer_manual_in',
        disabled=not in_options,
    )
    selected_out = out_options.get(out_label)
    selected_in = in_options.get(in_label)
    if st.button(
        'Сонгосон гүйлгээг хослох',
        disabled=selected_out is None or selected_in is None,
        key='transfer_manual_pair',
    ):
        try:
            if account_id is not None and account_id not in (
                selected_out['account_id'], selected_in['account_id']
            ):
                raise ValueError('Сонгосон дансны scope-д дор хаяж нэг тал нь багтах ёстой.')
            confirm_transfer_pair(conn, selected_out['id'], selected_in['id'])
            st.success('Шилжүүлгийн хос баталгаажлаа.')
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    pairs = confirmed_transfer_pairs(conn, currency)
    if pairs:
        st.markdown('**Баталгаажсан хосууд**')
        for pair in pairs:
            pair_label = (
                f"{pair['outgoing_date'][:10]} · "
                f"{_format_minor(pair['amount_minor'])} {currency_label} · "
                f"{pair['outgoing_account']} → {pair['incoming_account']}"
            )
            left, right = st.columns([5, 1])
            left.write(pair_label)
            if right.button('Хос салгах', key=f"unpair_transfer_{pair['id']}"):
                unpair_transfer(conn, pair['id'])
                st.success('Шилжүүлгийн хос салгагдлаа.')
                st.rerun()


def render(conn: sqlite3.Connection) -> None:
    """Filter transactions, edit categories and notes, and export results."""
    st.title('Гүйлгээнүүд')
    bounds = conn.execute(
        'SELECT MIN(txn_datetime), MAX(txn_datetime) FROM transactions'
    ).fetchone()
    if not bounds[0]:
        st.info('Эхлээд банкны хуулгаа оруулна уу.')
        return

    end_default = date.fromisoformat(bounds[1][:10])
    start_default = end_default - timedelta(days=89)
    category_rows = conn.execute(
        'SELECT id, name, is_active FROM categories ORDER BY name'
    ).fetchall()
    category_ids = {row['name']: row['id'] for row in category_rows}
    active_categories = [
        row['name'] for row in category_rows if row['is_active']
    ]

    with st.expander('Шүүлтүүр', expanded=True):
        left, right = st.columns(2)
        start = left.date_input(
            'Эхлэх огноо',
            start_default,
            key='transactions_from',
        )
        end = right.date_input(
            'Дуусах огноо',
            end_default,
            key='transactions_to',
        )
        chosen_categories = st.multiselect(
            'Ангилал',
            ['Ангилалгүй', *[category_label(name) for name in category_ids]],
            key='transactions_categories',
        )
        direction = st.selectbox(
            'Гүйлгээний чиглэл',
            ['All', 'In', 'Out'],
            format_func=lambda value: DIRECTION_LABELS[value],
            key='transactions_direction',
        )
        search = st.text_input(
            'Гүйлгээний утга, худалдаа үйлчилгээ эсвэл тэмдэглэлээс хайх',
            key='transactions_search',
        )
        currencies = available_currencies(conn)
        default_currency = 'MNT' if 'MNT' in currencies else currencies[0]
        currency = st.selectbox(
            'Гүйлгээний валют', currencies, index=currencies.index(default_currency),
            key='transactions_currency',
        )
        account_options = {'Бүх данс': None}
        account_options.update({
            str(row['label']): int(row['id'])
            for row in available_accounts(conn, currency)
        })
        account_label = st.selectbox(
            'Данс', list(account_options), key='transactions_account',
        )
        selected_account_id = account_options[account_label]
        currency_label = '₮' if currency == 'MNT' else currency
        left, right = st.columns(2)
        minimum = left.text_input(
            f'Хамгийн бага дүн ({currency_label})',
            key='transactions_minimum',
        )
        maximum = right.text_input(
            f'Хамгийн их дүн ({currency_label})',
            key='transactions_maximum',
        )
        unclassified = st.checkbox(
            'Зөвхөн ангилаагүй гүйлгээ',
            key='transactions_unclassified',
        )
        hide_fees = st.checkbox(
            'Банкны шимтгэлийн гүйлгээг нуух',
            key='transactions_hide_fees',
        )

    if start > end:
        st.error('Эхлэх огноо дуусах огнооноос хойш байж болохгүй.')
        return

    clauses, params = transaction_scope(
        start.isoformat(), end.isoformat(), selected_account_id, currency,
    )
    if chosen_categories:
        category_clauses: list[str] = []
        if 'Ангилалгүй' in chosen_categories:
            category_clauses.append('t.category_id IS NULL')
        selected_ids = [
            category_ids[category_name(name)]
            for name in chosen_categories
            if name != 'Ангилалгүй'
        ]
        if selected_ids:
            placeholders = ', '.join('?' for _ in selected_ids)
            category_clauses.append(f't.category_id IN ({placeholders})')
            params.extend(selected_ids)
        clauses.append('(' + ' OR '.join(category_clauses) + ')')

    if direction != 'All':
        clauses.append('t.direction = ?')
        params.append(direction.lower())
    if search:
        clauses.append(
            '(t.raw_description LIKE ? OR t.merchant_clean LIKE ? OR t.note LIKE ?)'
        )
        params.extend([f'%{search}%'] * 3)

    try:
        minimum_minor: int | None = None
        maximum_minor: int | None = None
        if minimum.strip():
            minimum_minor = to_minor(Decimal(minimum.strip()))
        if maximum.strip():
            maximum_minor = to_minor(Decimal(maximum.strip()))
        if minimum_minor is not None and minimum_minor < 0:
            raise ValueError
        if maximum_minor is not None and maximum_minor < 0:
            raise ValueError
        if (
            minimum_minor is not None
            and maximum_minor is not None
            and minimum_minor > maximum_minor
        ):
            st.error('Хамгийн бага дүн хамгийн их дүнгээс их байж болохгүй.')
            return
    except (ValueError, ArithmeticError):
        st.error('0-ээс багагүй дүнг зөв оруулна уу. Жишээ нь: 1500.50')
        return

    if minimum_minor is not None:
        clauses.append('ABS(t.amount) >= ?')
        params.append(minimum_minor)
    if maximum_minor is not None:
        clauses.append('ABS(t.amount) <= ?')
        params.append(maximum_minor)
    if unclassified:
        clauses.append("t.category_id IS NULL AND t.classified_by = 'none'")
    if hide_fees:
        clauses.append("(c.type IS NULL OR c.type != 'fee')")

    query = f'''SELECT t.id,
                       t.txn_datetime AS date,
                       t.amount,
                       t.direction,
                       t.raw_description AS description,
                       t.merchant_clean AS merchant,
                       t.counterparty_account AS counterparty,
                       COALESCE(c.name, 'Ангилалгүй') AS category,
                       t.note,
                       CASE WHEN c.type='transfer' OR EXISTS (
                           SELECT 1 FROM transfer_pairs tp
                           WHERE tp.outgoing_transaction_id=t.id
                              OR tp.incoming_transaction_id=t.id
                       ) THEN 1 ELSE 0 END AS is_transfer,
                       COUNT(*) OVER () AS filtered_count,
                       COALESCE(SUM(CASE WHEN t.direction='in'
                           AND (c.type IS NULL OR c.type!='transfer')
                           AND NOT EXISTS (SELECT 1 FROM transfer_pairs tp
                               WHERE tp.outgoing_transaction_id=t.id OR tp.incoming_transaction_id=t.id)
                           THEN t.amount ELSE 0 END) OVER (), 0) AS filtered_in,
                       COALESCE(SUM(CASE WHEN t.direction='out'
                           AND (c.type IS NULL OR c.type!='transfer')
                           AND NOT EXISTS (SELECT 1 FROM transfer_pairs tp
                               WHERE tp.outgoing_transaction_id=t.id OR tp.incoming_transaction_id=t.id)
                           THEN -t.amount ELSE 0 END) OVER (), 0) AS filtered_out,
                       COALESCE(SUM(CASE WHEN t.direction='out' AND c.type='fee'
                           AND NOT EXISTS (SELECT 1 FROM transfer_pairs tp
                               WHERE tp.outgoing_transaction_id=t.id OR tp.incoming_transaction_id=t.id)
                           THEN -t.amount ELSE 0 END) OVER (), 0) AS filtered_fees
                FROM transactions AS t
                LEFT JOIN categories AS c ON c.id = t.category_id
                LEFT JOIN accounts AS a ON a.id=t.account_id
                WHERE {' AND '.join(clauses)}
                ORDER BY t.txn_datetime DESC, t.id DESC
                LIMIT ?'''

    filter_key = hashlib.sha256(
        repr((clauses, params)).encode('utf-8')
    ).hexdigest()[:16]
    if st.session_state.get('transactions_filter_key') != filter_key:
        st.session_state['transactions_filter_key'] = filter_key
        st.session_state['transactions_limit'] = 500
    limit = st.session_state.get('transactions_limit', 500)
    result = read_sql_query(query, conn, params=[*params, limit])

    page_in = 0
    page_out = 0
    page_fees = 0
    if result.empty:
        st.info('Сонгосон шүүлтүүрт тохирох гүйлгээ алга.')
        filtered_count = 0
        total_in = 0
        total_out = 0
        total_fees = 0
        export_rows = pd.DataFrame(
            columns=[
                'id', 'date', 'amount', 'description', 'merchant',
                'counterparty', 'category', 'note', 'is_transfer',
            ]
        )
        editor_key = 'transactions_editor_empty_' + filter_key
        edited = pd.DataFrame(columns=[*export_rows.columns, 'Сонгох'])
    else:
        filtered_count = int(result.iloc[0]['filtered_count'])
        total_in = int(result.iloc[0]['filtered_in'])
        total_out = int(result.iloc[0]['filtered_out'])
        total_fees = int(result.iloc[0]['filtered_fees'])
        page_in = int(result.loc[(result['direction'] == 'in') & (result['is_transfer'] == 0), 'amount'].sum())
        page_out = int(-result.loc[(result['direction'] == 'out') & (result['is_transfer'] == 0), 'amount'].sum())
        page_fees = int(-result.loc[(result['direction'] == 'out') & (result['is_transfer'] == 0) & (result['category'] == 'Bank Fees & Charges'), 'amount'].sum())
        export_rows = result[
            [
                'id', 'date', 'amount', 'description', 'merchant',
                'counterparty', 'category', 'note', 'is_transfer',
            ]
        ].copy()
        export_rows['category'] = export_rows['category'].map(category_label)
        export_rows['amount'] = export_rows['amount'].map(
            lambda amount: _format_minor(int(amount)) + f' {currency_label}'
        )
        export_rows['is_transfer'] = export_rows['is_transfer'].map(
            lambda paired: 'Шилжүүлэг' if paired else ''
        )

        editor_rows = export_rows.copy()
        editor_rows.insert(0, 'Сонгох', False)
        editor_key_hash = hashlib.sha256(
            repr(
                tuple(
                    (row['id'], row['category'], row['note'])
                    for _, row in export_rows.iterrows()
                )
            ).encode('utf-8')
        ).hexdigest()[:16]
        editor_key = f'transactions_editor_{filter_key}_{editor_key_hash}'
        options = ['Ангилалгүй', *[category_label(name) for name in active_categories]]
        options.extend(
            category
            for category in export_rows['category'].unique()
            if category not in options
        )
        edited = st.data_editor(
            editor_rows,
            hide_index=True,
            key=editor_key,
            disabled=[
                'id', 'date', 'amount', 'description', 'merchant', 'counterparty',
                'is_transfer',
            ],
            column_config={
                'id': st.column_config.NumberColumn('Дугаар', format='%d'),
                'date': st.column_config.TextColumn('Огноо'),
                'amount': st.column_config.TextColumn(f'Дүн ({currency_label})'),
                'description': st.column_config.TextColumn('Гүйлгээний утга'),
                'merchant': st.column_config.TextColumn('Худалдаа, үйлчилгээ'),
                'counterparty': st.column_config.TextColumn('Харьцсан данс'),
                'is_transfer': st.column_config.TextColumn('Шилжүүлгийн төлөв'),
                'category': st.column_config.SelectboxColumn(
                    'Ангилал',
                    options=options,
                    required=True,
                ),
                'note': st.column_config.TextColumn('Тэмдэглэл'),
                'Сонгох': st.column_config.CheckboxColumn('Сонгох'),
            },
        )

    dirty_count = 0
    if not result.empty:
        for index, original in export_rows.iterrows():
            changed = edited.loc[index]
            original_note = '' if pd.isna(original['note']) else str(original['note'])
            changed_note = '' if pd.isna(changed['note']) else str(changed['note'])
            dirty_count += int(
                changed['category'] != original['category']
                or changed_note != original_note
            )
    st.caption(f'Хадгалаагүй өөрчлөлт: {dirty_count:,}')

    if 'transactions_saved_count' in st.session_state:
        saved_count = st.session_state.pop('transactions_saved_count')
        st.success(f'{saved_count} гүйлгээний өөрчлөлтийг хадгаллаа.')

    if st.button('Өөрчлөлтийг хадгалах', key='transactions_save_changes', disabled=dirty_count == 0):
        saved_count = 0
        if not result.empty:
            with conn:
                for index, original in export_rows.iterrows():
                    changed = edited.loc[index]
                    category_changed = changed['category'] != original['category']
                    note = '' if pd.isna(changed['note']) else str(changed['note'])
                    original_note = (
                        '' if pd.isna(original['note']) else str(original['note'])
                    )
                    note_changed = note != original_note
                    if not category_changed and not note_changed:
                        continue
                    txn_id = int(original['id'])
                    if category_changed:
                        category_id = (
                            None
                            if changed['category'] == 'Ангилалгүй'
                            else category_ids[category_name(changed['category'])]
                        )
                        set_category(conn, [txn_id], category_id)
                    if note_changed:
                        conn.execute(
                            'UPDATE transactions SET note = ? WHERE id = ?',
                            (note, txn_id),
                        )
                    saved_count += 1
        st.session_state['transactions_saved_count'] = saved_count
        st.rerun()

    bulk_categories = [
        'Ангилалгүй',
        *[category_label(name) for name in active_categories],
    ]
    bulk_category = st.selectbox(
        'Сонгосон гүйлгээний ангилал',
        bulk_categories,
        key='transactions_bulk_category',
    )
    remember = st.selectbox(
        'Дүрэм болгон хадгалах',
        ['Үгүй', 'Энэ худалдаа, үйлчилгээ', 'Энэ харьцсан данс'],
        key='transactions_bulk_remember',
    )
    category_id = (
        None if bulk_category == 'Ангилалгүй'
        else category_ids[category_name(bulk_category)]
    )
    remember_value = {
        'Үгүй': None,
        'Энэ худалдаа, үйлчилгээ': 'merchant',
        'Энэ харьцсан данс': 'counterparty',
    }[remember]
    selected_ids = (
        [int(row['id']) for _, row in edited.iterrows() if bool(row['Сонгох'])]
        if not result.empty else []
    )
    st.caption(f'{len(selected_ids):,} гүйлгээ сонгогдсон')
    scope_preview = None
    preview_error = None
    if selected_ids and remember_value and category_id is not None:
        try:
            scope_preview = preview_rule_scope(
                conn, selected_ids, remember_value, category_id
            )
            preview_total = (scope_preview['total'] + 50) // 100
            st.caption(
                f"Дүрмийн хамрах хүрээ: {scope_preview['count']} гүйлгээ · "
                f"{preview_total:,} {currency_label} · {DIRECTION_LABELS[scope_preview['direction']]} · "
                f"жишээ: {scope_preview['sample_desc'] or 'утгагүй'}"
            )
        except ValueError as exc:
            preview_error = str(exc)
            st.warning(preview_error)
    if st.button(
        'Сонгосон гүйлгээнд хэрэглэх', key='transactions_apply_selected',
        disabled=bool(remember_value and (not selected_ids or category_id is None or preview_error)),
    ):
        category_id = (
            None
            if bulk_category == 'Ангилалгүй'
            else category_ids[category_name(bulk_category)]
        )
        remember_value = {
            'Үгүй': None,
            'Энэ худалдаа, үйлчилгээ': 'merchant',
            'Энэ харьцсан данс': 'counterparty',
        }[remember]
        try:
            with conn:
                updated_count = set_category(
                    conn, selected_ids, category_id, remember_value,
                )
            st.success(f'{updated_count} гүйлгээг шинэчиллээ.')
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    if st.button(
        'Нэмж 500 гүйлгээ харуулах',
        disabled=filtered_count <= limit,
        key='transactions_show_more',
    ):
        st.session_state['transactions_limit'] = limit + 500
        st.rerun()
    st.caption(f'{filtered_count} гүйлгээнээс {len(result)}-ийг харуулж байна')

    st.subheader('Энэ хуудсын дүн')
    page_metrics = st.columns(2)
    page_metrics[0].metric(f'Орлого ({currency_label})', _format_minor(page_in))
    page_metrics[1].metric(f'Зарлага ({currency_label})', _format_minor(page_out))
    st.caption(f'Энэ хуудсанд {currency_label} { _format_minor(page_fees) } банкны шимтгэл зарлагад багтсан.')
    st.subheader('Шүүлтүүрт нийт дүн')
    total_metrics = st.columns(2)
    total_metrics[0].metric(f'Нийт орлого ({currency_label})', _format_minor(total_in))
    total_metrics[1].metric(f'Нийт зарлага ({currency_label})', _format_minor(total_out))
    st.metric(f'Үүнээс банкны шимтгэл ({currency_label})', _format_minor(total_fees))
    st.caption('Нийт дүнд баталгаажсан шилжүүлгийг хассан. Шимтгэл зарлагад хэвээр орно.')
    csv_rows = export_rows.rename(
        columns={
            'id': 'Дугаар',
            'date': 'Огноо',
            'amount': f'Дүн ({currency_label})',
            'description': 'Гүйлгээний утга',
            'merchant': 'Худалдаа, үйлчилгээ',
            'counterparty': 'Харьцсан данс',
            'category': 'Ангилал',
            'note': 'Тэмдэглэл',
        }
    )
    st.download_button(
        'Шүүсэн гүйлгээг CSV файл болгон татах',
        csv_rows.to_csv(index=False).encode('utf-8-sig'),
        'guigeenuud.csv',
        'text/csv',
        key='transactions_export',
    )

    _render_transfer_tools(
        conn, start, end, selected_account_id, currency,
    )
