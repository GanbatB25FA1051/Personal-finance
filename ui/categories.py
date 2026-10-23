"""Category management with protected system categories."""

import sqlite3

import pandas as pd
import streamlit as st
from core.classify import set_category
from core.db import read_sql_query
from ui import (
    category_label,
    category_name,
    category_type_label,
)

PROTECTED = {'Bank Fees & Charges', 'Transfers (own accounts)'}
CATEGORY_TYPES = ['income', 'expense', 'transfer', 'fee']
CLASSIFICATION_LABELS = {
    'rule': 'Автомат дүрэм',
    'system': 'Системийн ангилал',
    'manual': 'Гараар ангилсан',
    'none': 'Ангилаагүй',
}


def render(conn: sqlite3.Connection) -> None:
    """Add, rename, archive and safely delete categories."""
    st.title('Ангиллын жагсаалт')
    table = read_sql_query(
        '''SELECT c.id,
                  c.name,
                  c.type,
                  COUNT(t.id) AS usage,
                  c.is_active AS active,
                  c.is_default
           FROM categories AS c
           LEFT JOIN transactions AS t ON t.category_id = c.id
           GROUP BY c.id
           ORDER BY c.type, c.name''',
        conn,
    )
    if not table.empty:
        table['name'] = table['name'].map(category_label)
        table['type'] = table['type'].map(category_type_label)
        table['active'] = table['active'].astype(bool)
    st.dataframe(
        table,
        hide_index=True,
        column_config={
            'id': st.column_config.NumberColumn('Дугаар', format='%d'),
            'name': 'Ангиллын нэр',
            'type': 'Төрөл',
            'usage': st.column_config.NumberColumn(
                'Холбогдсон гүйлгээ',
                format='%d',
            ),
            'active': st.column_config.CheckboxColumn('Идэвхтэй'),
            'is_default': st.column_config.CheckboxColumn('Үндсэн ангилал'),
        },
    )

    with st.form('new_category'):
        name = st.text_input('Шинэ ангиллын нэр')
        category_type = st.selectbox(
            'Төрөл',
            CATEGORY_TYPES,
            format_func=category_type_label,
        )
        submitted = st.form_submit_button('Ангилал нэмэх')
        if submitted:
            cleaned_name = name.strip()
            if not cleaned_name or cleaned_name in {
                'Uncategorized',
                'Ангилалгүй',
            }:
                st.error('Ангиллын нэр оруулна уу. "Ангилалгүй" гэж нэрлэж болохгүй.')
            else:
                try:
                    with conn:
                        conn.execute(
                            '''INSERT INTO categories(name, type, is_default)
                               VALUES (?, ?, 0)''',
                            (cleaned_name, category_type),
                        )
                    st.success('Ангилал нэмэгдлээ.')
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error('Ийм нэртэй ангилал аль хэдийн байна.')

    rows = {
        row['name']: row
        for row in conn.execute('SELECT * FROM categories ORDER BY name')
    }
    if not rows:
        return

    chosen = st.selectbox(
        'Засах ангиллаа сонгоно уу',
        list(rows),
        format_func=category_label,
        key='manage_category',
    )
    row = rows[chosen]
    category_id = int(row['id'])
    protected = chosen in PROTECTED
    renamed = st.text_input(
        'Шинэ нэр',
        value=category_label(chosen),
        key=f'rename_category_{category_id}',
        disabled=protected,
    )
    if st.button(
        'Нэр өөрчлөх',
        disabled=protected,
        key=f'rename_button_{category_id}',
    ):
        cleaned_name = category_name(renamed.strip())
        if not cleaned_name or cleaned_name in {'Uncategorized', 'Ангилалгүй'}:
            st.error('Ангиллын нэр оруулна уу. "Ангилалгүй" гэж нэрлэж болохгүй.')
        elif cleaned_name == chosen:
            st.info('Ангиллын нэр өөрчлөгдсөнгүй.')
        else:
            try:
                with conn:
                    conn.execute(
                        'UPDATE categories SET name=? WHERE id=?',
                        (cleaned_name, category_id),
                    )
                st.rerun()
            except sqlite3.IntegrityError:
                st.error('Ийм нэртэй ангилал аль хэдийн байна.')

    if st.button(
        'Архивлах' if row['is_active'] else 'Архиваас сэргээх',
        disabled=protected,
        key=f'toggle_category_{category_id}',
    ):
        with conn:
            conn.execute(
                'UPDATE categories SET is_active=? WHERE id=?',
                (0 if row['is_active'] else 1, category_id),
            )
        st.rerun()

    has_transactions = conn.execute(
        'SELECT 1 FROM transactions WHERE category_id=? LIMIT 1',
        (category_id,),
    ).fetchone() is not None
    has_rules = conn.execute(
        'SELECT 1 FROM merchant_rules WHERE category_id=? LIMIT 1',
        (category_id,),
    ).fetchone() is not None
    can_delete = not row['is_default'] and not has_transactions and not has_rules
    if st.button(
        'Ашиглагдаагүй ангиллыг устгах',
        disabled=not can_delete,
        key=f'delete_category_{category_id}',
    ):
        with conn:
            conn.execute('DELETE FROM categories WHERE id=?', (category_id,))
        st.rerun()
    if not can_delete:
        st.caption(
            'Үндсэн ангилал болон гүйлгээ, дүрэмд ашигласан ангиллыг устгах '
            'боломжгүй. Ашиглахаа больсон бол архивлана уу.'
        )

    st.divider()
    st.subheader('Гүйлгээний ангиллыг гараар засах')
    st.caption(
        'Автоматаар дүрмээр ангилсан гүйлгээг нэг бүрчлэн засаж болно. '
        'Хүсвэл ижил дүрэмтэй бусад гүйлгээ болон дараагийн хуулгад хэрэглэх '
        'дүрмийг хамт шинэчилнэ.'
    )
    transaction_category_options = ['Бүх ангилал', 'Ангилалгүй'] + [
        category_label(name) for name in rows
    ]
    transaction_category = st.selectbox(
        'Харах гүйлгээний ангилал',
        transaction_category_options,
        key='category_transaction_filter',
    )
    transaction_source = st.selectbox(
        'Ангилсан арга',
        ['Бүгд', 'Автомат дүрэм', 'Системийн ангилал', 'Гараар ангилсан'],
        key='category_transaction_source',
    )
    transaction_clauses: list[str] = []
    transaction_params: list[int] = []
    if transaction_category == 'Ангилалгүй':
        transaction_clauses.append('t.category_id IS NULL')
    elif transaction_category != 'Бүх ангилал':
        stored_name = category_name(transaction_category)
        selected_category_id = int(rows[stored_name]['id'])
        transaction_clauses.append('t.category_id=?')
        transaction_params.append(selected_category_id)

    source_values = {
        'Автомат дүрэм': 'rule',
        'Системийн ангилал': 'system',
        'Гараар ангилсан': 'manual',
    }
    if transaction_source != 'Бүгд':
        transaction_clauses.append('t.classified_by=?')
        transaction_params.append(source_values[transaction_source])
    where_clause = (
        'WHERE ' + ' AND '.join(transaction_clauses)
        if transaction_clauses
        else ''
    )
    transaction_rows = read_sql_query(
        f'''SELECT t.id, t.txn_datetime AS date, t.amount,
                   t.merchant_clean AS merchant,
                   t.raw_description AS description,
                   c.name AS category,
                   t.classified_by,
                   t.rule_id
            FROM transactions AS t
            LEFT JOIN categories AS c ON c.id=t.category_id
            {where_clause}
            ORDER BY t.txn_datetime DESC, t.id DESC
            LIMIT 500''',
        conn,
        params=transaction_params,
    )
    if transaction_rows.empty:
        st.info('Энэ шүүлтүүрт тохирох гүйлгээ алга.')
    else:
        if len(transaction_rows) == 500:
            st.warning(
                '500 хүртэлх гүйлгээг харуулж байна. Шүүлтүүрээ нарийсгана уу.'
            )
        transaction_rows['amount_display'] = transaction_rows['amount'].map(
            lambda amount: (
                f"{'-' if amount < 0 else ''}"
                f"{abs(int(amount)) // 100:,}."
                f"{abs(int(amount)) % 100:02d} ₮"
            )
        )
        transaction_rows['category'] = transaction_rows['category'].fillna(
            'Ангилалгүй'
        ).map(
            lambda name: (
                'Ангилалгүй' if name == 'Ангилалгүй' else category_label(name)
            )
        )
        transaction_rows['classified_by'] = transaction_rows[
            'classified_by'
        ].map(CLASSIFICATION_LABELS)
        display_rows = transaction_rows[
            [
                'id',
                'date',
                'amount_display',
                'merchant',
                'description',
                'category',
                'classified_by',
                'rule_id',
            ]
        ].copy()
        category_editor_options = [
            'Ангилалгүй',
            *[category_label(name) for name in rows],
        ]
        editor_key = (
            'category_assignment_editor_'
            f'{transaction_category}_{transaction_source}_'
            f'{transaction_rows["id"].iloc[0]}_'
            f'{transaction_rows["id"].iloc[-1]}'
        )
        edited_rows = st.data_editor(
            display_rows,
            hide_index=True,
            key=editor_key,
            disabled=[
                'id',
                'date',
                'amount_display',
                'merchant',
                'description',
                'classified_by',
                'rule_id',
            ],
            column_config={
                'id': st.column_config.NumberColumn('Дугаар', format='%d'),
                'date': 'Огноо',
                'amount_display': 'Дүн',
                'merchant': 'Худалдаа, үйлчилгээ',
                'description': 'Гүйлгээний утга',
                'category': st.column_config.SelectboxColumn(
                    'Ангилал (засах)',
                    options=category_editor_options,
                    required=True,
                ),
                'classified_by': 'Ангилсан арга',
                'rule_id': st.column_config.NumberColumn(
                    'Дүрмийн дугаар',
                    format='%d',
                ),
            },
        )
        update_linked_rule = st.checkbox(
            'Ижил дүрмээр автоматаар ангилсан бусад гүйлгээ болон '
            'цаашдын хуулгад мөн хэрэглэх',
            key='update_linked_category_rule',
        )
        if st.button(
            'Ангиллын өөрчлөлтийг хадгалах',
            key='save_category_assignment_edits',
        ):
            changes: list[tuple[int, int | None, int | None, str]] = []
            for index, original in display_rows.iterrows():
                new_label = edited_rows.loc[index, 'category']
                if new_label == original['category']:
                    continue
                new_category_id = (
                    None
                    if new_label == 'Ангилалгүй'
                    else int(rows[category_name(new_label)]['id'])
                )
                rule_id = (
                    None
                    if pd.isna(original['rule_id'])
                    else int(original['rule_id'])
                )
                changes.append(
                    (
                        int(original['id']),
                        new_category_id,
                        rule_id,
                        str(original['classified_by']),
                    )
                )
            if not changes:
                st.info('Ангиллын өөрчлөлт алга.')
            else:
                rule_targets: dict[int, int] = {}
                conflict = False
                if update_linked_rule:
                    for _, new_category_id, rule_id, source in changes:
                        if source != CLASSIFICATION_LABELS['rule'] or rule_id is None:
                            continue
                        if new_category_id is None:
                            st.error(
                                'Автомат дүрэмтэй гүйлгээг “Ангилалгүй” болгох '
                                'үед дүрмийг хамтад нь шинэчлэх боломжгүй. '
                                'Дүрэм шинэчлэх сонголтыг болиулж хадгална уу.'
                            )
                            conflict = True
                            break
                        existing_target = rule_targets.get(rule_id)
                        if (
                            existing_target is not None
                            and existing_target != new_category_id
                        ):
                            st.error(
                                'Нэг дүрэмд холбогдсон мөрүүдийг өөр өөр ангилалд '
                                'зассан байна. Нэг дүрмийн мөрүүдэд ижил ангилал '
                                'сонгоно уу.'
                            )
                            conflict = True
                            break
                        rule_targets[rule_id] = new_category_id
                if not conflict:
                    saved_count = 0
                    with conn:
                        for txn_id, new_category_id, rule_id, source in changes:
                            if update_linked_rule and rule_id in rule_targets:
                                continue
                            saved_count += set_category(
                                conn,
                                [txn_id],
                                new_category_id,
                            )
                        for rule_id, new_category_id in rule_targets.items():
                            conn.execute(
                                '''UPDATE merchant_rules
                                   SET category_id=?, is_active=1
                                   WHERE id=?''',
                                (new_category_id, rule_id),
                            )
                            saved_count += conn.execute(
                                '''UPDATE transactions
                                   SET category_id=?
                                   WHERE rule_id=? AND classified_by='rule' ''',
                                (new_category_id, rule_id),
                            ).rowcount
                            conn.execute(
                                '''UPDATE ai_category_mappings
                                   SET category_id=?
                                   WHERE rule_id=?''',
                                (new_category_id, rule_id),
                            )
                    st.success(
                        f'{saved_count} гүйлгээний ангиллыг шинэчиллээ.'
                    )
                    st.rerun()
