"""Merchant rule management."""

import sqlite3

import pandas as pd
import streamlit as st

from core.classify import (
    add_rule,
    apply_rules,
    classify_remaining,
    learn_rules_from_manual,
)
from core.db import read_sql_query
from ui import (
    DIRECTION_LABELS,
    MATCH_TYPE_LABELS,
    category_label,
    category_name,
    translate_rule_error,
)


def render(conn: sqlite3.Connection) -> None:
    """Add, edit, delete and apply merchant classification rules."""
    st.title('Ангиллын дүрмүүд')
    active_categories = {
        row['name']: row['id']
        for row in conn.execute(
            'SELECT id, name FROM categories WHERE is_active=1 ORDER BY name'
        )
    }
    all_categories = {
        row['name']: row['id']
        for row in conn.execute('SELECT id, name FROM categories ORDER BY name')
    }

    with st.form('add_rule'):
        match_type = st.selectbox(
            'Тохирлын төрөл',
            ['merchant_exact', 'counterparty_account', 'contains', 'regex'],
            format_func=lambda value: MATCH_TYPE_LABELS[value],
        )
        pattern = st.text_input('Хайх утга')
        direction = st.selectbox(
            'Гүйлгээний чиглэл',
            ['any', 'in', 'out'],
            format_func=lambda value: DIRECTION_LABELS[value],
        )
        category_names = list(active_categories)
        if category_names:
            category = st.selectbox(
                'Ангилал',
                category_names,
                format_func=category_label,
            )
        else:
            category = None
            st.selectbox(
                'Ангилал',
                ['Идэвхтэй ангилал алга'],
                disabled=True,
            )
        priority = st.number_input(
            'Дараалал (бага тоо эхэлж хэрэгжинэ)',
            value=100,
            step=1,
        )
        submitted = st.form_submit_button(
            'Дүрэм нэмэх',
            disabled=not active_categories,
        )
        if submitted and category is not None:
            try:
                with conn:
                    add_rule(
                        conn,
                        match_type,
                        pattern,
                        active_categories[category],
                        direction,
                        int(priority),
                    )
                st.success('Дүрэм нэмэгдлээ.')
                st.rerun()
            except ValueError as exc:
                st.error(translate_rule_error(str(exc)))

    rules = read_sql_query(
        '''SELECT r.id,
                  r.match_type,
                  r.pattern,
                  r.direction_filter AS direction,
                  c.name AS category,
                  CASE WHEN EXISTS (
                      SELECT 1 FROM ai_category_mappings AS a
                      WHERE a.rule_id=r.id
                  ) THEN 'AI' ELSE 'Хэрэглэгч' END AS source,
                  r.priority,
                  r.is_active AS active,
                  r.hit_count AS hits
           FROM merchant_rules AS r
           JOIN categories AS c ON c.id = r.category_id
           ORDER BY r.priority, r.id''',
        conn,
    )
    if not rules.empty:
        rules['active'] = rules['active'].astype(bool)
        rules['match_type'] = rules['match_type'].map(MATCH_TYPE_LABELS)
        rules['direction'] = rules['direction'].map(DIRECTION_LABELS)
        rules['category'] = rules['category'].map(category_label)
        rules.insert(0, 'Устгах', False)
        category_options = [category_label(name) for name in all_categories]
        edited = st.data_editor(
            rules,
            hide_index=True,
            key='rule_editor',
            disabled=[
                'id', 'match_type', 'pattern', 'direction', 'source', 'hits',
            ],
            column_config={
                'Устгах': st.column_config.CheckboxColumn(
                    'Устгах'
                ),
                'id': st.column_config.NumberColumn('Дугаар', format='%d'),
                'match_type': st.column_config.TextColumn('Тохирлын төрөл'),
                'pattern': st.column_config.TextColumn('Хайх утга'),
                'direction': st.column_config.TextColumn('Чиглэл'),
                'source': st.column_config.TextColumn('Эх үүсвэр'),
                'hits': st.column_config.NumberColumn(
                    'Хэрэглэсэн тоо',
                    format='%d',
                ),
                'category': st.column_config.SelectboxColumn(
                    'Ангилал',
                    options=category_options,
                    required=True,
                ),
                'priority': st.column_config.NumberColumn(
                    'Дараалал',
                    step=1,
                    required=True,
                ),
                'active': st.column_config.CheckboxColumn('Идэвхтэй'),
            },
        )

        if st.button('Дүрмийн өөрчлөлтийг хадгалах', key='save_rule_changes'):
            try:
                with conn:
                    for _, row in edited.iterrows():
                        stored_category = category_name(row['category'])
                        if stored_category not in all_categories:
                            raise ValueError('Ангиллын сонголт буруу байна.')
                        priority_value = int(row['priority'])
                        conn.execute(
                            '''UPDATE merchant_rules
                               SET priority=?, is_active=?, category_id=?
                               WHERE id=?''',
                            (
                                priority_value,
                                int(bool(row['active'])),
                                all_categories[stored_category],
                                int(row['id']),
                            ),
                        )
                st.success('Дүрмийн өөрчлөлтийг хадгаллаа.')
                st.rerun()
            except (ValueError, TypeError, OverflowError) as exc:
                st.error(str(exc) or 'Зөв дарааллын тоо оруулна уу.')

        confirm_delete = st.checkbox(
            'Сонгосон дүрмүүдийг устгахыг баталгаажуулна уу',
            key='confirm_rule_deletion',
        )
        if st.button(
            'Сонгосон дүрмийг устгах',
            disabled=not confirm_delete,
            key='delete_selected_rules',
        ):
            selected_ids = [
                int(row['id'])
                for _, row in edited.iterrows()
                if bool(row['Устгах'])
            ]
            with conn:
                for rule_id in selected_ids:
                    conn.execute(
                        'UPDATE transactions SET rule_id=NULL WHERE rule_id=?',
                        (rule_id,),
                    )
                    conn.execute(
                        'DELETE FROM merchant_rules WHERE id=?',
                        (rule_id,),
                    )
            st.rerun()
    else:
        st.info('Одоогоор дүрэм нэмээгүй байна.')

    st.caption(
        'Гараар ангилсан гүйлгээний ижил утгуудаас дүрэм сурна. '
        'Дүрэм таараагүй орлогыг “Бусад орлого”, зарлагыг '
        '“Бусад зарлага” ангилалд автоматаар оруулна.'
    )
    if st.button(
        'Гараар ангилсан гүйлгээнээс суралцаж, автоматаар ангилах',
        key='learn_and_classify',
    ):
        with conn:
            learned_count = learn_rules_from_manual(conn)
            rule_count = apply_rules(conn)
            fallback_count = classify_remaining(conn)
        st.success(
            f'{learned_count} шинэ дүрэм сурлаа. '
            f'{rule_count} гүйлгээг дүрмээр, '
            f'{fallback_count} гүйлгээг бусад ангилалд орууллаа.'
        )

    if st.button(
        'Ангилаагүй гүйлгээг автоматаар ангилах',
        key='rerun_unclassified_rules',
    ):
        with conn:
            rule_count = apply_rules(conn)
            fallback_count = classify_remaining(conn)
        st.success(
            f'{rule_count} гүйлгээг дүрмээр, '
            f'{fallback_count} гүйлгээг бусад ангилалд орууллаа.'
        )

    confirm_reapply = st.checkbox(
        'Зөвхөн дүрмээр ангилсан гүйлгээг дахин ангилахыг баталгаажуулна уу',
        key='confirm_rule_reapply',
    )
    if st.button(
        'Гараар ангилаагүй бүх гүйлгээнд дүрмийг дахин хэрэглэх',
        disabled=not confirm_reapply,
        key='reapply_all_rules',
    ):
        with conn:
            conn.execute(
                """UPDATE transactions
                   SET category_id=NULL, rule_id=NULL, classified_by='none'
                   WHERE classified_by='rule'"""
            )
            count = apply_rules(conn)
        st.success(f'{count} гүйлгээг дүрмээр дахин ангиллаа.')
