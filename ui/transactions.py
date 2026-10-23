"""Filtered transaction browsing, manual editing and CSV export."""

import hashlib
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import streamlit as st

from core.classify import set_category
from core.models import to_minor
from ui import DIRECTION_LABELS, category_label, category_name


def _format_minor(amount: int) -> str:
    """Format signed minor units as a two-decimal amount."""
    sign = '-' if amount < 0 else ''
    units, cents = divmod(abs(amount), 100)
    return f'{sign}{units}.{cents:02d}'


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
        left, right = st.columns(2)
        minimum = left.text_input(
            'Хамгийн бага дүн (₮)',
            key='transactions_minimum',
        )
        maximum = right.text_input(
            'Хамгийн их дүн (₮)',
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

    clauses = [
        'substr(t.txn_datetime, 1, 10) >= ?',
        'substr(t.txn_datetime, 1, 10) <= ?',
    ]
    params: list[object] = [start.isoformat(), end.isoformat()]
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
                       t.raw_description AS description,
                       t.merchant_clean AS merchant,
                       t.counterparty_account AS counterparty,
                       COALESCE(c.name, 'Ангилалгүй') AS category,
                       t.note,
                       COUNT(*) OVER () AS filtered_count,
                       COALESCE(SUM(CASE WHEN t.direction = 'in'
                                         THEN t.amount ELSE 0 END) OVER (), 0)
                           AS filtered_in,
                       COALESCE(SUM(CASE WHEN t.direction = 'out'
                                         THEN -t.amount ELSE 0 END) OVER (), 0)
                           AS filtered_out
                FROM transactions AS t
                LEFT JOIN categories AS c ON c.id = t.category_id
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
    result = pd.read_sql_query(query, conn, params=[*params, limit])

    if result.empty:
        st.info('Сонгосон шүүлтүүрт тохирох гүйлгээ алга.')
        filtered_count = 0
        total_in = 0
        total_out = 0
        export_rows = pd.DataFrame(
            columns=[
                'id', 'date', 'amount', 'description', 'merchant',
                'counterparty', 'category', 'note',
            ]
        )
        editor_key = 'transactions_editor_empty_' + filter_key
        edited = pd.DataFrame(columns=[*export_rows.columns, 'Сонгох'])
    else:
        filtered_count = int(result.iloc[0]['filtered_count'])
        total_in = int(result.iloc[0]['filtered_in'])
        total_out = int(result.iloc[0]['filtered_out'])
        export_rows = result[
            [
                'id', 'date', 'amount', 'description', 'merchant',
                'counterparty', 'category', 'note',
            ]
        ].copy()
        export_rows['category'] = export_rows['category'].map(category_label)
        export_rows['amount'] = export_rows['amount'].map(_format_minor)

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
            ],
            column_config={
                'id': st.column_config.NumberColumn('Дугаар', format='%d'),
                'date': st.column_config.TextColumn('Огноо'),
                'amount': st.column_config.TextColumn('Дүн (₮)'),
                'description': st.column_config.TextColumn('Гүйлгээний утга'),
                'merchant': st.column_config.TextColumn('Худалдаа, үйлчилгээ'),
                'counterparty': st.column_config.TextColumn('Харьцсан данс'),
                'category': st.column_config.SelectboxColumn(
                    'Ангилал',
                    options=options,
                    required=True,
                ),
                'note': st.column_config.TextColumn('Тэмдэглэл'),
                'Сонгох': st.column_config.CheckboxColumn('Сонгох'),
            },
        )

    if 'transactions_saved_count' in st.session_state:
        saved_count = st.session_state.pop('transactions_saved_count')
        st.success(f'{saved_count} гүйлгээний өөрчлөлтийг хадгаллаа.')

    if st.button('Өөрчлөлтийг хадгалах', key='transactions_save_changes'):
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
    if st.button('Сонгосон гүйлгээнд хэрэглэх', key='transactions_apply_selected'):
        selected_ids = (
            [
                int(row['id'])
                for _, row in edited.iterrows()
                if bool(row['Сонгох'])
            ]
            if not result.empty
            else []
        )
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
        with conn:
            updated_count = set_category(
                conn,
                selected_ids,
                category_id,
                remember_value,
            )
        st.success(f'{updated_count} гүйлгээг шинэчиллээ.')
        st.rerun()

    if st.button(
        'Нэмж 500 гүйлгээ харуулах',
        disabled=filtered_count <= limit,
        key='transactions_show_more',
    ):
        st.session_state['transactions_limit'] = limit + 500
        st.rerun()
    st.caption(f'{filtered_count} гүйлгээнээс {len(result)}-ийг харуулж байна')

    left, right = st.columns(2)
    left.metric('Шүүсэн орлого (₮)', _format_minor(total_in))
    right.metric('Шүүсэн зарлага (₮)', _format_minor(total_out))
    csv_rows = export_rows.rename(
        columns={
            'id': 'Дугаар',
            'date': 'Огноо',
            'amount': 'Дүн (₮)',
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
