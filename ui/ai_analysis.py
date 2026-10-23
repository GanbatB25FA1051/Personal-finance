"""Optional OpenAI analysis of expenses for a selected period."""

import os
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from core.ai_classify import (
    AIClassificationError,
    AUTO_APPLY_CONFIDENCE,
    analyze_expenses,
)
from core.db import backup_db, init_db, read_sql_query
from ui import category_label

APP_DIR = Path(__file__).resolve().parent.parent


def render(conn: sqlite3.Connection) -> None:
    """Analyze local expenses and show saved category mappings."""
    init_db(conn)
    st.title('AI-аар зарлагад дүн шинжилгээ хийх')
    st.warning(
        'Энэ сонголтыг ажиллуулахад зөвхөн гүйлгээний огноо, дүн, '
        'гүйлгээний утгын хэсэг, давтамж болон зарлагын ангиллын нэрс '
        'OpenAI API руу илгээгдэнэ. Дансны дугаар, үлдэгдэл, эзэмшигчийн '
        'тусдаа талбар, эх Excel файл илгээгдэхгүй. Гэхдээ эдгээр мэдээлэл '
        'гүйлгээний утга дотор бичигдсэн бол текстийн хамт илгээгдэж болно.'
    )
    bounds = conn.execute(
        '''SELECT MIN(substr(txn_datetime, 1, 10)),
                  MAX(substr(txn_datetime, 1, 10))
           FROM transactions WHERE direction='out' '''
    ).fetchone()
    if not bounds[0]:
        st.info('Шинжлэх зарлагын гүйлгээ алга. Эхлээд хуулгаа оруулна уу.')
        return

    left, right = st.columns(2)
    start = left.date_input(
        'Эхлэх огноо',
        date.fromisoformat(bounds[0]),
        key='ai_analysis_start',
    )
    end = right.date_input(
        'Дуусах огноо',
        date.fromisoformat(bounds[1]),
        key='ai_analysis_end',
    )
    st.caption(
        f'{AUTO_APPLY_CONFIDENCE:.0%} ба түүнээс дээш итгэлтэй '
        'саналыг дүрэм болгон хадгалж автоматаар хэрэглэнэ. Бусад саналыг '
        'дүрэмд нэмэхгүй, ангилах жагсаалтад шалгуулахаар үлдээнэ.'
    )

    if not os.environ.get('OPENAI_API_KEY', '').strip():
        st.info(
            'AI ашиглахын тулд Windows-ийн OPENAI_API_KEY орчны хувьсагчид '
            'API түлхүүрээ тохируулаад аппыг дахин эхлүүлнэ үү. '
            'Түлхүүрээ чат эсвэл кодонд бүү бичээрэй.'
        )
    if st.button(
        'Сонгосон хугацааг AI-аар шинжлүүлэх',
        disabled=start > end,
        key='run_ai_expense_analysis',
    ):
        try:
            backup_db(
                str(APP_DIR / 'finance.db'),
                str(APP_DIR / 'backups'),
            )
            with st.spinner('Зарлагын утгуудыг шинжилж байна...'):
                result = analyze_expenses(conn, start.isoformat(), end.isoformat())
            st.session_state['ai_analysis_result'] = result
            st.success(
                f"{result['transactions']:,} гүйлгээг шинжилж, "
                f"{result['mappings']:,} төрлийн ангиллын зураглал үүсгэлээ. "
                f"{result['auto_classified']:,} гүйлгээнд AI дүрэм хэрэглэж, "
                f"{result['needs_review']:,} гүйлгээг гараар шалгах жагсаалтад үлдээлээ."
            )
        except (AIClassificationError, ValueError) as exc:
            st.error(str(exc))
    elif start > end:
        st.error('Эхлэх огноо дуусах огнооноос хойш байж болохгүй.')

    st.subheader('AI ангиллын зураглал')
    runs = conn.execute(
        '''SELECT id, period_start, period_end, created_at, transaction_count,
                  mapping_count, auto_classified, needs_review
           FROM ai_analysis_runs
           ORDER BY created_at DESC, id DESC''',
    )
    run_options = {
        (
            f"#{row['id']} · {row['period_start']} – {row['period_end']} · "
            f"{row['transaction_count']:,} гүйлгээ · {row['created_at']}"
        ): row
        for row in runs
        if row['period_start'] == start.isoformat()
        and row['period_end'] == end.isoformat()
    }
    if not run_options:
        st.info(
            'Энэ хугацаанд AI шинжилгээний зураглал алга. '
            'Дээрх товчоор шинжилгээг эхлүүлнэ үү.'
        )
        return
    selected_run_label = st.selectbox(
        'Энэ хугацааны шинжилгээ',
        list(run_options),
        key='ai_analysis_run',
    )
    selected_run = run_options[selected_run_label]
    run_id = int(selected_run['id'])
    cols = st.columns(4)
    cols[0].metric('Шинжилсэн гүйлгээ', f"{selected_run['transaction_count']:,}")
    cols[1].metric('Зарлагын зураглал', f"{selected_run['mapping_count']:,}")
    cols[2].metric('Автоматаар ангилсан', f"{selected_run['auto_classified']:,}")
    cols[3].metric('Шалгах шаардлагатай', f"{selected_run['needs_review']:,}")

    mappings = read_sql_query(
        '''SELECT m.match_value AS merchant,
                  c.name AS category,
                  m.expense_total_minor,
                  m.transaction_count,
                  m.confidence,
                  m.reason
           FROM ai_expense_mappings AS m
           JOIN categories AS c ON c.id=m.category_id
           WHERE m.run_id=?
           ORDER BY m.expense_total_minor DESC, m.match_value''',
        conn,
        params=[run_id],
    )
    if mappings.empty:
        st.info('Сонгосон шинжилгээнд зураглах зарлага байхгүй байна.')
        return
    total_expense_minor = int(mappings['expense_total_minor'].sum())
    mappings['amount_mnt'] = mappings['expense_total_minor'].map(
        lambda amount: amount / 100
    )
    mappings['share'] = mappings['expense_total_minor'].map(
        lambda amount: (
            f'{amount / total_expense_minor:.1%}'
            if total_expense_minor
            else '0.0%'
        )
    )
    mappings['category'] = mappings['category'].map(category_label)
    mappings['confidence'] = mappings['confidence'].map(
        lambda value: f'{value:.0%}'
    )
    st.metric('Шинжилсэн нийт зарлага', f'{total_expense_minor / 100:,.2f} ₮')
    category_totals = (
        mappings.groupby('category', as_index=False)['amount_mnt']
        .sum()
        .sort_values('amount_mnt', ascending=False)
    )
    st.subheader('Ангиллаар хаашаа зарцуулсан')
    st.bar_chart(category_totals.set_index('category'), sort=False)
    st.dataframe(
        category_totals,
        hide_index=True,
        column_config={
            'category': 'Зарлагын ангилал',
            'amount_mnt': st.column_config.NumberColumn(
                'Нийт зарцуулсан (₮)',
                format='%,.2f',
            ),
        },
    )
    st.subheader('Худалдаа, үйлчилгээ тус бүрийн зураглал')
    st.dataframe(
        mappings[
            [
                'merchant',
                'category',
                'amount_mnt',
                'transaction_count',
                'share',
                'confidence',
                'reason',
            ]
        ],
        hide_index=True,
        column_config={
            'merchant': 'Худалдаа, үйлчилгээ / утгын зураглал',
            'category': 'Зарлагын ангилал',
            'amount_mnt': st.column_config.NumberColumn(
                'Зарцуулсан дүн (₮)',
                format='%,.2f',
            ),
            'transaction_count': st.column_config.NumberColumn(
                'Гүйлгээний тоо',
                format='%d',
            ),
            'share': 'Нийт зарлагад эзлэх хувь',
            'confidence': 'AI итгэлцэл',
            'reason': 'Үндэслэл',
        },
    )
