"""Consent-gated AI suggestions, review, and explicit approval."""

import os
import sqlite3
from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from core.ai_classify import (
    AIClassificationError,
    AUTO_APPLY_CONFIDENCE,
    analyze_expenses,
    approve_ai_suggestions,
    mask_sensitive_text,
    prepare_expense_preview,
)
from core.db import init_db, read_sql_query
from ui import category_label, category_name


def _safe_label(value: object) -> str:
    return mask_sensitive_text(value)


def render(conn: sqlite3.Connection) -> None:
    """Preview the minimized payload, obtain consent, then review suggestions."""
    init_db(conn)
    st.title('AI саналын хяналт')
    st.warning(
        'AI-д зөвхөн MNT зарлагын огноо, нэгтгэсэн дүн, гүйлгээний утгын '
        'маскласан хэсэг болон идэвхтэй зарлагын ангиллын нэр илгээнэ. '
        'Дансны үлдэгдэл, workbook, credential илгээхгүй. Масклах нь түгээмэл '
        'IBAN, дансны дугаар, утас, имэйлийн хэвийг танина; бүрэн anonymization биш.'
    )
    bounds = conn.execute(
        '''SELECT MIN(substr(txn_datetime, 1, 10)), MAX(substr(txn_datetime, 1, 10))
           FROM transactions WHERE direction='out' '''
    ).fetchone()
    if not bounds[0]:
        st.info('Шинжлэх зарлагын гүйлгээ алга. Эхлээд хуулгаа оруулна уу.')
        return

    left, right = st.columns(2)
    start = left.date_input('Эхлэх огноо', date.fromisoformat(bounds[0]), key='ai_analysis_start')
    end = right.date_input('Дуусах огноо', date.fromisoformat(bounds[1]), key='ai_analysis_end')
    scope = (start.isoformat(), end.isoformat())
    preview_key = 'ai_payload_preview'
    if st.button('AI-д илгээх мэдээллийг урьдчилан харах', key='ai_preview_payload', disabled=start > end):
        try:
            preview = prepare_expense_preview(conn, *scope)
            st.session_state[preview_key] = preview
            st.session_state['ai_external_consent'] = False
            st.session_state['ai_analysis_auto_apply'] = False
        except (AIClassificationError, ValueError) as exc:
            st.error(str(exc))

    preview = st.session_state.get(preview_key)
    preview_matches = bool(
        preview and (preview.get('start'), preview.get('end')) == scope
    )
    if preview_matches:
        st.subheader('Илгээх payload')
        st.caption(
            f"Загвар: {preview['model']} · {preview['transactions']:,} гүйлгээ · "
            f"{preview['mappings']:,} бүлэг · зөвхөн MNT"
        )
        if preview['mappings']:
            st.json(preview['payload'], expanded=False)
        else:
            st.info('Энэ хугацаанд санал үүсгэх MNT зарлага алга.')
        key_present = bool(os.environ.get('OPENAI_API_KEY', '').strip())
        if not key_present:
            st.info('OPENAI_API_KEY тохируулаагүй тул сүлжээний хүсэлт хийх боломжгүй.')
        consent = st.checkbox(
            'Дээрх маскласан payload-ийг OpenAI руу илгээхийг зөвшөөрч байна',
            key='ai_external_consent',
            disabled=not bool(preview['mappings']) or not key_present,
        )
        auto_apply = st.checkbox(
            'Өндөр confidence (≥85%) саналыг дүрэм болгон автоматаар хэрэглэх',
            value=False,
            key='ai_analysis_auto_apply',
            help='Энэ нь тусдаа, анхнаасаа унтраалттай сонголт. Сонгоогүй бол AI санал дүрэм үүсгэхгүй.',
            disabled=not bool(preview['mappings']),
        )
        st.caption(
            'Confidence нь загварын өөрийн үнэлгээ болохоос зөв байх баталгаатай магадлал биш. '
            'Анхдагч урсгал: санал → шалгах → таны батлах.'
        )
        if st.button(
            'Зөвшөөрөөд AI санал авах',
            key='run_ai_expense_analysis',
            disabled=not consent or not preview['mappings'] or not key_present,
        ):
            try:
                with st.spinner('Маскласан зарлагын бүлгүүдэд санал авч байна...'):
                    result = analyze_expenses(
                        conn, *scope, consent=True,
                        preview_fingerprint=str(preview['fingerprint']),
                        auto_apply=bool(auto_apply),
                    )
                st.session_state['ai_selected_run_id'] = result['run_id']
                st.success(
                    f"{result['transactions']:,} гүйлгээнд {result['mappings']:,} санал бэлэн. "
                    f"{result['auto_classified']:,} гүйлгээ хэрэглэгдсэн, "
                    f"{result['needs_review']:,} шалгах хэвээр байна."
                )
            except (AIClassificationError, ValueError) as exc:
                st.error(str(exc))
    elif preview:
        st.info('Огнооны хүрээ өөрчлөгдсөн тул шинэ payload preview гаргаж зөвшөөрнө үү.')
    elif start > end:
        st.error('Эхлэх огноо дуусах огнооноос хойш байж болохгүй.')

    st.subheader('AI саналын жагсаалт')
    runs = conn.execute(
        '''SELECT id, period_start, period_end, created_at, transaction_count,
                  mapping_count, auto_classified, needs_review
           FROM ai_analysis_runs WHERE period_start=? AND period_end=?
           ORDER BY created_at DESC, id DESC''',
        scope,
    ).fetchall()
    if not runs:
        st.info('Энэ хугацаанд AI санал алга. Payload-г эхлээд урьдчилан хараарай.')
        return
    run_options = {
        f"#{row['id']} · {row['period_start']} – {row['period_end']} · {row['transaction_count']:,} гүйлгээ · {row['created_at']}": int(row['id'])
        for row in runs
    }
    preferred_run = int(st.session_state.get('ai_selected_run_id', runs[0]['id']))
    option_values = list(run_options.values())
    default_index = option_values.index(preferred_run) if preferred_run in option_values else 0
    selected_label = st.selectbox(
        'Шинжилгээ', list(run_options), index=default_index, key='ai_review_run',
    )
    run_id = run_options[selected_label]
    selected_run = next(row for row in runs if int(row['id']) == run_id)
    metrics = st.columns(3)
    metrics[0].metric('Санал', f"{selected_run['mapping_count']:,}")
    metrics[1].metric('Хэрэглэсэн', f"{selected_run['auto_classified']:,}")
    metrics[2].metric('Шалгах', f"{selected_run['needs_review']:,}")

    suggestions = read_sql_query(
        '''SELECT m.id AS mapping_id, m.match_value, m.category_id, c.name AS category,
                  m.approved_category_id, ac.name AS approved_category,
                  m.expense_total_minor, m.transaction_count, m.confidence, m.reason,
                  m.applied_count
           FROM ai_expense_mappings m
           JOIN categories c ON c.id=m.category_id
           LEFT JOIN categories ac ON ac.id=m.approved_category_id
           WHERE m.run_id=?
           ORDER BY m.expense_total_minor DESC, m.match_value''',
        conn, params=[run_id],
    )
    if suggestions.empty:
        st.info('Саналын мөр алга.')
        return
    active_categories = conn.execute(
        "SELECT id, name FROM categories WHERE is_active=1 AND type='expense' ORDER BY name"
    ).fetchall()
    category_by_label = {category_label(row['name']): int(row['id']) for row in active_categories}
    category_labels = list(category_by_label)
    for suggested_name in suggestions['category'].dropna().unique():
        suggested_label = category_label(suggested_name)
        if suggested_label not in category_labels:
            category_labels.append(suggested_label)
    review_frame = pd.DataFrame({
        'mapping_id': suggestions['mapping_id'].astype(int),
        'Сонгох': False,
        'Худалдаа, үйлчилгээ': suggestions['match_value'].map(_safe_label),
        'AI санал': suggestions['category'].map(category_label),
        'Батлах ангилал': suggestions.apply(
            lambda row: category_label(
                row['category'] if pd.isna(row['approved_category']) else row['approved_category']
            ), axis=1,
        ),
        'Дүн (₮)': suggestions['expense_total_minor'].map(
            lambda value: f'{Decimal(int(value)) / Decimal(100):,.2f} ₮'
        ),
        'Гүйлгээ': suggestions['transaction_count'].astype(int),
        'Confidence': suggestions['confidence'].map(lambda value: f'{float(value):.0%}'),
        'Үндэслэл': suggestions['reason'].map(_safe_label),
        'Хэрэглэсэн': suggestions['applied_count'].astype(int),
    })
    edited = st.data_editor(
        review_frame,
        hide_index=True,
        key=f'ai_suggestion_review_{run_id}',
        disabled=['mapping_id', 'Худалдаа, үйлчилгээ', 'AI санал', 'Дүн (₮)', 'Гүйлгээ', 'Confidence', 'Үндэслэл', 'Хэрэглэсэн'],
        column_order=['Сонгох', 'Худалдаа, үйлчилгээ', 'AI санал', 'Батлах ангилал', 'Дүн (₮)', 'Гүйлгээ', 'Confidence', 'Үндэслэл', 'Хэрэглэсэн'],
        column_config={
            'Батлах ангилал': st.column_config.SelectboxColumn('Батлах ангилал', options=category_labels, required=True),
            'Дүн (₮)': st.column_config.TextColumn('Дүн (₮)'),
            'Confidence': st.column_config.TextColumn('Confidence'),
            'Гүйлгээ': st.column_config.NumberColumn('Гүйлгээ', format='%d'),
            'Сонгох': st.column_config.CheckboxColumn('Санал батлах'),
        },
    )
    decisions = []
    for _, row in edited.iterrows():
        if bool(row['Сонгох']):
            category_id = category_by_label.get(str(row['Батлах ангилал']))
            if category_id is None:
                st.error('Ангиллын сонголт хүчингүй байна.')
                return
            decisions.append({'mapping_id': int(row['mapping_id']), 'category_id': category_id})
    st.caption(f'{len(decisions):,} санал батлахаар сонгосон')
    if st.button('Сонгосон саналыг баталж дүрэм болгох', key=f'ai_approve_{run_id}', disabled=not decisions):
        try:
            result = approve_ai_suggestions(conn, run_id, decisions)
            st.success(
                f"{result['approved']:,} саналыг баталлаа. "
                f"{result['auto_classified']:,} гүйлгээнд дүрэм хэрэглэгдлээ."
            )
            st.rerun()
        except (AIClassificationError, ValueError) as exc:
            st.error(str(exc))
