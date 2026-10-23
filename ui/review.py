"""Grouped transaction review queue."""
import hashlib
import re
import sqlite3

import pandas as pd
import streamlit as st

from core.classify import get_review_groups, set_category
from ui import category_label


def _extract_transferor_name(description: str | None) -> str | None:
    """Extract the transferor name from Khan Bank transfer descriptions."""
    if not description:
        return None
    match = re.search(
        r'\bШилжүүлэгч\s+(.+?)\s+ХААНААС\s*:',
        description,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def _transaction_details(
    conn: sqlite3.Connection,
    txn_ids: list[int],
) -> pd.DataFrame:
    """Load detailed transaction rows in SQLite-safe batches."""
    rows: list[sqlite3.Row] = []
    for offset in range(0, len(txn_ids), 900):
        batch = txn_ids[offset:offset + 900]
        placeholders = ','.join('?' for _ in batch)
        rows.extend(
            conn.execute(
                f'''SELECT txn_datetime, amount, raw_description,
                           counterparty_account, branch_code
                    FROM transactions
                    WHERE id IN ({placeholders})
                    ORDER BY txn_datetime, id''',
                batch,
            ).fetchall()
        )

    details = pd.DataFrame(
        [
            {
                'Огноо': row['txn_datetime'],
                'Дүн': (
                    f"{'-' if row['amount'] < 0 else ''}"
                    f"{abs(row['amount']) // 100:,}."
                    f"{abs(row['amount']) % 100:02d} ₮"
                ),
                'Гүйлгээний утга': row['raw_description'] or 'Бүртгээгүй',
                'Шилжүүлэгчийн овог нэр': (
                    _extract_transferor_name(row['raw_description'])
                    or 'Утгад байхгүй'
                ),
                'Харьцсан данс': row['counterparty_account'] or 'Бүртгээгүй',
                'Салбарын код': row['branch_code'] or 'Бүртгээгүй',
            }
            for row in rows
        ],
        columns=[
            'Огноо',
            'Дүн',
            'Гүйлгээний утга',
            'Шилжүүлэгчийн овог нэр',
            'Харьцсан данс',
            'Салбарын код',
        ],
    )
    return details


def render(conn: sqlite3.Connection) -> None:
    """Review groups individually or classify several groups together."""
    st.title('Гүйлгээ ангилах')
    skipped = st.session_state.setdefault('skipped_groups', set())
    groups = [(hashlib.sha256(repr((g['key_type'], g['key'], g['direction'], g['txn_ids'])).encode()).hexdigest(), g)
              for g in get_review_groups(conn)]
    groups = [(key, g) for key, g in groups if key not in skipped]
    if st.button('Алгассан бүлгүүдийг харуулах'):
        skipped.clear()
        st.rerun()
    if not groups:
        st.success('Харуулах гүйлгээг бүгдийг ангилсан байна.')
        return
    page = min(st.session_state.get('review_page', 0), (len(groups)-1)//15)
    current_groups = groups[page*15:(page+1)*15]
    for key, group in current_groups:
        with st.container(border=True):
            st.subheader(group['key'])
            st.checkbox(
                'Багцаар ангилахаар сонгох',
                key='select_' + key,
            )
            total_mnt = (group['total'] + 50) // 100
            direction_label = 'Орлого' if group['direction'] == 'in' else 'Зарлага'
            st.write(
                f"{direction_label} · {group['count']} гүйлгээ · "
                f'{total_mnt:,} ₮'
            )
            details = _transaction_details(conn, group['txn_ids'])
            accounts = sorted(
                {
                    account
                    for account in details['Харьцсан данс']
                    if account != 'Бүртгээгүй'
                }
            )
            account_label = (
                'Хэнээс орж ирсэн данс'
                if group['direction'] == 'in'
                else 'Хаашаа шилжүүлсэн данс'
            )
            st.write(
                f'**{account_label}:** '
                + (', '.join(accounts) if accounts else 'Хуулгад бүртгээгүй байна.')
            )
            with st.expander('Гүйлгээний дэлгэрэнгүйг харах'):
                st.dataframe(details, hide_index=True)
            kinds = ('income', 'transfer') if group['direction'] == 'in' else ('expense', 'transfer', 'fee')
            categories = {
                category_label(row['name']): row['id']
                for row in conn.execute(
                    'SELECT * FROM categories WHERE is_active=1 ORDER BY name'
                )
                if row['type'] in kinds
            }
            category_names = list(categories) or ['Идэвхтэй ангилал алга']
            chosen = st.selectbox('Ангилал', category_names, key='cat_'+key,
                                  disabled=not categories)
            remember = st.selectbox(
                'Дүрэм болгон хадгалах',
                ['Үгүй', 'Энэ худалдаа, үйлчилгээ', 'Энэ харьцсан данс'],
                key='remember_'+key,
            )
            left, right = st.columns(2)
            if left.button('Хадгалах', key='save_'+key, disabled=not categories):
                with conn:
                    set_category(conn, group['txn_ids'], categories[chosen],
                                 {
                                     'Үгүй': None,
                                     'Энэ худалдаа, үйлчилгээ': 'merchant',
                                     'Энэ харьцсан данс': 'counterparty',
                                 }[remember])
                st.rerun()
            if right.button('Алгасах', key='skip_'+key):
                skipped.add(key)
                st.rerun()

    selected_groups = [
        (key, group)
        for key, group in current_groups
        if st.session_state.get('select_' + key, False)
    ]
    if selected_groups:
        selected_types = [
            {'income', 'transfer'}
            if group['direction'] == 'in'
            else {'expense', 'transfer', 'fee'}
            for _, group in selected_groups
        ]
        compatible_types = set.intersection(*selected_types)
        bulk_categories = {
            category_label(row['name']): row['id']
            for row in conn.execute(
                'SELECT id, name, type FROM categories '
                'WHERE is_active=1 ORDER BY name'
            )
            if row['type'] in compatible_types
        }
        if bulk_categories:
            st.subheader('Олон бүлгийг нэг дор ангилах')
            st.caption(
                f'{len(selected_groups)} бүлгийн '
                f'{sum(group["count"] for _, group in selected_groups)} '
                'гүйлгээг ижил ангилалд оруулна.'
            )
            bulk_category = st.selectbox(
                'Сонгосон бүлгүүдийн ангилал',
                list(bulk_categories),
                key='review_bulk_category',
            )
            if st.button(
                'Сонгосон бүлгийг бүгдийг хадгалах',
                key='review_bulk_save',
            ):
                transaction_ids = [
                    txn_id
                    for _, group in selected_groups
                    for txn_id in group['txn_ids']
                ]
                with conn:
                    changed = set_category(
                        conn,
                        transaction_ids,
                        bulk_categories[bulk_category],
                    )
                st.success(
                    f'{len(selected_groups)} бүлгийн '
                    f'{changed} гүйлгээг ангиллаа.'
                )
                st.rerun()
    prev, label, nxt = st.columns(3)
    if prev.button('Өмнөх', disabled=page == 0):
        st.session_state['review_page'] = page-1
        st.rerun()
    label.write(f'{page+1} / {(len(groups)-1)//15+1}-р хуудас')
    if nxt.button('Дараах', disabled=(page+1)*15 >= len(groups)):
        st.session_state['review_page'] = page+1
        st.rerun()
