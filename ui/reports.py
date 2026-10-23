"""Interactive financial dashboard and locally computed insights."""

import hashlib
import sqlite3
from datetime import date

import pandas as pd
import streamlit as st

from core.db import read_sql_query
from ui import category_label

REPORT_SECTIONS = {
    'classification': 'Ангилалтын явц',
    'monthly': 'Сарын орлого, зарлага',
    'categories': 'Ангиллаар зарлага',
    'merchants': 'Худалдаа, үйлчилгээ',
    'transactions': 'Гүйлгээнүүдийг шүүх, сонгох',
    'insights': 'Дотоод ухаалаг дүн шинжилгээ',
}
MAX_VISIBLE_TRANSACTIONS = 2000


def _load_dashboard_data(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    selected_categories: list[str],
    classification_filter: str,
    import_id: int | None = None,
) -> pd.DataFrame:
    """Load transactions within the selected dashboard filters."""
    clauses = [
        'substr(t.txn_datetime, 1, 10) >= ?',
        'substr(t.txn_datetime, 1, 10) <= ?',
    ]
    params: list[object] = [start, end]
    if import_id is not None:
        clauses.append(
            '''EXISTS (
                   SELECT 1 FROM import_transactions AS it
                   WHERE it.transaction_id=t.id AND it.import_id=?
               )'''
        )
        params.append(import_id)
    category_rows = conn.execute(
        'SELECT id, name FROM categories ORDER BY name'
    ).fetchall()
    category_ids = {row['name']: row['id'] for row in category_rows}
    if selected_categories and 'Бүгд' not in selected_categories:
        selected_ids = [
            category_ids[name]
            for name in selected_categories
            if name != 'Ангилалгүй'
        ]
        category_clauses: list[str] = []
        if 'Ангилалгүй' in selected_categories:
            category_clauses.append('t.category_id IS NULL')
        if selected_ids:
            placeholders = ','.join('?' for _ in selected_ids)
            category_clauses.append(f't.category_id IN ({placeholders})')
            params.extend(selected_ids)
        clauses.append('(' + ' OR '.join(category_clauses) + ')')

    if classification_filter == 'Гараар ангилсан':
        clauses.append("t.classified_by='manual'")
    elif classification_filter == 'Автоматаар ангилсан':
        clauses.append("t.classified_by IN ('rule','system')")
    elif classification_filter == 'Ангилаагүй':
        clauses.append("t.category_id IS NULL AND t.classified_by='none'")

    return read_sql_query(
        f'''SELECT t.id, t.txn_datetime AS date, t.amount,
                   t.direction, t.raw_description AS description,
                   t.merchant_clean AS merchant,
                   t.counterparty_account AS counterparty,
                   t.note, t.category_id,
                   COALESCE(c.name, 'Ангилалгүй') AS category,
                   COALESCE(c.type, '') AS category_type,
                   t.classified_by
            FROM transactions AS t
            LEFT JOIN categories AS c ON c.id=t.category_id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.txn_datetime DESC, t.id DESC''',
        conn,
        params=params,
    )


def _format_minor(amount: int) -> str:
    """Format signed minor units as MNT."""
    sign = '-' if amount < 0 else ''
    units, cents = divmod(abs(int(amount)), 100)
    return f'{sign}{units:,}.{cents:02d} ₮'


def _monthly_summary(data: pd.DataFrame) -> pd.DataFrame:
    """Summarize filtered non-transfer transactions by month."""
    eligible = data[data['category_type'] != 'transfer'].copy()
    if eligible.empty:
        return pd.DataFrame(columns=['month', 'income', 'expense', 'net'])
    eligible['month'] = eligible['date'].str[:7]
    eligible['income_minor'] = eligible['amount'].clip(lower=0)
    eligible['expense_minor'] = -eligible['amount'].clip(upper=0)
    summary = eligible.groupby('month', as_index=False).agg(
        income_minor=('income_minor', 'sum'),
        expense_minor=('expense_minor', 'sum'),
    )
    summary['income'] = summary['income_minor'] / 100
    summary['expense'] = summary['expense_minor'] / 100
    summary['net'] = summary['income'] - summary['expense']
    return summary[['month', 'income', 'expense', 'net']]


def _spending_by_category(data: pd.DataFrame) -> pd.DataFrame:
    """Aggregate filtered outgoing non-transfer transactions."""
    expenses = data[
        (data['amount'] < 0) & (data['category_type'] != 'transfer')
    ].copy()
    if expenses.empty:
        return pd.DataFrame(columns=['category', 'total'])
    expenses['total_minor'] = -expenses['amount']
    spending = expenses.groupby('category', as_index=False).agg(
        total_minor=('total_minor', 'sum')
    )
    spending['total'] = spending['total_minor'] / 100
    return (
        spending[['category', 'total']]
        .sort_values(['total', 'category'], ascending=[False, True])
        .reset_index(drop=True)
    )


def _top_merchants(data: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    """Rank filtered outgoing non-transfer merchants."""
    expenses = data[
        (data['amount'] < 0) & (data['category_type'] != 'transfer')
    ].copy()
    if expenses.empty:
        return pd.DataFrame(columns=['merchant', 'total', 'count'])
    expenses['merchant'] = expenses['merchant'].replace('', 'Ангилалгүй').fillna(
        'Ангилалгүй'
    )
    expenses['amount_minor'] = -expenses['amount']
    merchants = expenses.groupby('merchant', as_index=False).agg(
        amount_minor=('amount_minor', 'sum'),
        count=('id', 'count'),
    )
    merchants['total'] = merchants['amount_minor'] / 100
    return (
        merchants[['merchant', 'total', 'count']]
        .sort_values(['total', 'merchant'], ascending=[False, True])
        .head(limit)
        .reset_index(drop=True)
    )


def _show_insights(
    monthly: pd.DataFrame,
    spending: pd.DataFrame,
    fees_minor: int,
    automatic: int,
    manual: int,
    unclassified: int,
    total_count: int,
) -> None:
    """Present deterministic insights calculated locally from filtered rows."""
    st.subheader(REPORT_SECTIONS['insights'])
    if monthly.empty:
        st.info('Шинжлэх орлого, зарлагын гүйлгээ алга.')
    else:
        latest = monthly.iloc[-1]
        st.write(
            f"Сүүлийн сарын цэвэр орлого: **{latest['net']:,.2f} ₮** "
            f"(орлого {latest['income']:,.2f} ₮, зарлага "
            f"{latest['expense']:,.2f} ₮)."
        )
        if len(monthly) > 1:
            previous = monthly.iloc[-2]
            change = latest['expense'] - previous['expense']
            if previous['expense']:
                percent = change / previous['expense'] * 100
                st.write(
                    f"Зарлага өмнөх сараас **{abs(percent):,.1f}% "
                    f"{'өссөн' if change > 0 else 'буурсан' if change < 0 else 'өөрчлөгдөөгүй'}**."
                )
            else:
                st.write(
                    f"Сарын зарлага өмнөх сарын "
                    f"{previous['expense']:,.2f} ₮-өөс "
                    f"{latest['expense']:,.2f} ₮ боллоо."
                )
    if not spending.empty:
        largest = spending.iloc[0]
        st.write(
            f"Хамгийн их зарлагатай ангилал: "
            f"**{category_label(largest['category'])}** — "
            f"{largest['total']:,.2f} ₮."
        )
    if total_count:
        classified = automatic + manual
        st.write(
            f"Ангилалтын явц: **{classified:,}/{total_count:,}** "
            f"({classified / total_count:.0%}); ангилаагүй "
            f"**{unclassified:,}**. Банкны шимтгэл: "
            f"**{_format_minor(fees_minor)}**."
        )
    st.caption(
        'Эдгээр зөвлөмжийг энэ төхөөрөмж дээрх өгөгдлөөс тооцно. '
        'OpenAI эсвэл өөр гадаад үйлчилгээ рүү мэдээлэл илгээхгүй.'
    )


def render(conn: sqlite3.Connection) -> None:
    """Display a selectable dashboard, transaction picker, and local insights."""
    st.title('Санхүүгийн тайлан')
    import_rows = conn.execute(
        '''SELECT i.id, i.filename, i.period_start, i.period_end, i.imported_at,
                  i.rows_parsed, i.rows_duplicate,
                  COUNT(it.transaction_id) AS linked_transactions
           FROM imports AS i
           LEFT JOIN import_transactions AS it ON it.import_id=i.id
           GROUP BY i.id, i.filename, i.period_start, i.period_end,
                    i.imported_at, i.rows_parsed, i.rows_duplicate
           ORDER BY i.id DESC'''
    ).fetchall()
    import_options: dict[str, int | None] = {'Бүх хуулгыг нэгтгэх': None}
    import_details: dict[int, sqlite3.Row] = {}
    for row in import_rows:
        period = ' – '.join(
            value for value in (row['period_start'], row['period_end']) if value
        ) or 'хугацаа тодорхойгүй'
        label = (
            f"#{row['id']} · {row['filename']} · {period} · "
            f"{row['imported_at']}"
        )
        import_options[label] = row['id']
        import_details[row['id']] = row
    selected_label = st.selectbox(
        'Шинжилгээний хамрах хүрээ',
        list(import_options),
        key='reports_import_scope',
    )
    selected_import_id = import_options[selected_label]
    if selected_import_id is None:
        bounds = conn.execute(
            'SELECT MIN(txn_datetime), MAX(txn_datetime) FROM transactions'
        ).fetchone()
    else:
        bounds = conn.execute(
            '''SELECT MIN(t.txn_datetime), MAX(t.txn_datetime)
               FROM transactions AS t
               JOIN import_transactions AS it ON it.transaction_id=t.id
               WHERE it.import_id=?''',
            (selected_import_id,),
        ).fetchone()
        selected_import = import_details[selected_import_id]
        if (
            int(selected_import['rows_duplicate'] or 0) > 0
            and int(selected_import['linked_transactions'] or 0)
            < int(selected_import['rows_parsed'] or 0)
        ):
            st.warning(
                'Энэ хуулгыг өмнөх хувилбарт импортлоход давхардсан мөрийн '
                'эх үүсвэр хадгалагдаагүй тул одоо дангаар нь шинжлэхэд зарим '
                'давхардсан гүйлгээ дутуу байж болно. Шинээр оруулах хуулгууд '
                'энэ холбоосыг бүрэн хадгална.'
            )
    if not bounds[0]:
        st.info('Тайлан гаргах гүйлгээ алга. Эхлээд банкны хуулгаа оруулна уу.')
        return

    left, right = st.columns(2)
    start = left.date_input(
        'Эхлэх огноо',
        date.fromisoformat(bounds[0][:10]),
        key='reports_start',
    )
    end = right.date_input(
        'Дуусах огноо',
        date.fromisoformat(bounds[1][:10]),
        key='reports_end',
    )
    if start > end:
        st.error('Эхлэх огноо дуусах огнооноос хойш байж болохгүй.')
        return

    sections = st.multiselect(
        'Dashboard-д харуулах хэсгүүд',
        list(REPORT_SECTIONS),
        default=list(REPORT_SECTIONS),
        format_func=lambda section: REPORT_SECTIONS[section],
        key='reports_sections',
    )
    category_names = [
        row['name']
        for row in conn.execute('SELECT name FROM categories ORDER BY name')
    ]
    selected_categories = st.multiselect(
        'Ангиллаар тайлан шүүх',
        ['Бүгд', 'Ангилалгүй', *[category_label(name) for name in category_names]],
        default=['Бүгд'],
        key='reports_categories',
    )
    classification_filter = st.selectbox(
        'Гүйлгээний ангилалтын төлөв',
        ['Бүгд', 'Гараар ангилсан', 'Автоматаар ангилсан', 'Ангилаагүй'],
        key='reports_classification_filter',
    )
    data = _load_dashboard_data(
        conn,
        start.isoformat(),
        end.isoformat(),
        [
            next(
                (name for name in category_names if category_label(name) == label),
                label,
            )
            for label in selected_categories
        ],
        classification_filter,
        import_id=selected_import_id,
    )
    if data.empty:
        st.info('Сонгосон шүүлтүүрт тохирох гүйлгээ алга.')
        return

    eligible = data[data['category_type'] != 'transfer']
    monthly = _monthly_summary(data)
    spending = _spending_by_category(data)
    merchants = _top_merchants(data)
    fees_minor = int(
        -data.loc[
            (data['category_type'] == 'fee') & (data['amount'] < 0),
            'amount',
        ].sum()
    )
    automatic = int(data['classified_by'].isin(['rule', 'system']).sum())
    manual = int((data['classified_by'] == 'manual').sum())
    unclassified = int(
        (
            data['category_id'].isna()
            & (data['classified_by'] == 'none')
            & (data['category_type'] != 'fee')
        ).sum()
    )

    if 'classification' in sections:
        st.subheader(REPORT_SECTIONS['classification'])
        cols = st.columns(4)
        cols[0].metric('Нийт гүйлгээ', f'{len(data):,}')
        cols[1].metric('Автоматаар ангилсан', f'{automatic:,}')
        cols[2].metric('Гараар ангилсан', f'{manual:,}')
        cols[3].metric('Ангилаагүй', f'{unclassified:,}')
        if len(eligible):
            st.progress(
                min(1.0, (automatic + manual) / len(eligible)),
                text='Ангилсан гүйлгээний хувь',
            )

    if 'monthly' in sections:
        st.subheader(REPORT_SECTIONS['monthly'])
        if monthly.empty:
            st.info('Шилжүүлгээс бусад орлого, зарлага алга.')
        else:
            total_income = int(eligible['amount'].clip(lower=0).sum())
            total_expense = int(-eligible['amount'].clip(upper=0).sum())
            cols = st.columns(4)
            cols[0].metric('Орлого', _format_minor(total_income))
            cols[1].metric('Зарлага', _format_minor(total_expense))
            cols[2].metric('Цэвэр орлого', _format_minor(total_income - total_expense))
            cols[3].metric('Банкны шимтгэл', _format_minor(fees_minor))
            monthly_chart = monthly.set_index('month')[
                ['income', 'expense']
            ].rename(columns={'income': 'Орлого', 'expense': 'Зарлага'})
            monthly_chart.index.name = 'Сар'
            st.bar_chart(monthly_chart)
            st.dataframe(
                monthly,
                hide_index=True,
                column_config={
                    'month': 'Сар',
                    'income': st.column_config.NumberColumn('Орлого (₮)', format='%,.2f'),
                    'expense': st.column_config.NumberColumn('Зарлага (₮)', format='%,.2f'),
                    'net': st.column_config.NumberColumn('Цэвэр (₮)', format='%,.2f'),
                },
            )

    if 'categories' in sections:
        st.subheader(REPORT_SECTIONS['categories'])
        if spending.empty:
            st.info('Сонгосон хугацаанд зарлага алга.')
        else:
            chart = spending.copy()
            chart['category'] = chart['category'].map(category_label)
            st.bar_chart(chart.set_index('category'), sort=False)
            st.dataframe(
                chart,
                hide_index=True,
                column_config={
                    'category': 'Ангилал',
                    'total': st.column_config.NumberColumn(
                        'Нийт зарлага (₮)',
                        format='%,.2f',
                    ),
                },
            )
            category_months = data[
                (data['amount'] < 0) & (data['category_type'] != 'transfer')
            ].copy()
            if not category_months.empty:
                category_months['month'] = category_months['date'].str[:7]
                category_months['expense'] = -category_months['amount'] / 100
                trend = category_months.pivot_table(
                    index='month',
                    columns='category',
                    values='expense',
                    aggfunc='sum',
                    fill_value=0,
                )
                trend.columns = [
                    category_label(name) for name in trend.columns
                ]
                st.subheader('Ангиллаарх сарын зарлагын өөрчлөлт')
                st.line_chart(trend)

    if 'merchants' in sections:
        st.subheader('Зарлага ихтэй 10 худалдаа, үйлчилгээ')
        st.dataframe(
            merchants,
            hide_index=True,
            column_config={
                'merchant': 'Худалдаа, үйлчилгээ',
                'total': st.column_config.NumberColumn(
                    'Нийт зарлага (₮)',
                    format='%,.2f',
                ),
                'count': st.column_config.NumberColumn('Гүйлгээний тоо', format='%d'),
            },
        )

    if 'transactions' in sections:
        st.subheader(REPORT_SECTIONS['transactions'])
        if len(data) > MAX_VISIBLE_TRANSACTIONS:
            st.warning(
                f'Шүүлтүүрт {len(data):,} гүйлгээ байна. Сонгон нийлбэрлэх '
                f'хүснэгтэд эхний {MAX_VISIBLE_TRANSACTIONS:,} гүйлгээ харагдана; '
                'огноо эсвэл ангиллын шүүлтүүрээ нарийсгана уу.'
            )
        rows = data.head(MAX_VISIBLE_TRANSACTIONS).copy()
        rows['date'] = rows['date'].str[:19]
        rows['amount_display'] = rows['amount'].map(_format_minor)
        rows['category'] = rows['category'].map(category_label)
        display = rows[
            ['id', 'date', 'direction', 'amount_display', 'merchant',
             'description', 'category', 'classified_by']
        ].copy()
        display.insert(0, 'Сонгох', False)
        filter_hash = hashlib.sha256(
            repr((
                start.isoformat(),
                end.isoformat(),
                selected_categories,
                classification_filter,
                selected_import_id,
                rows['id'].tolist(),
            )).encode('utf-8')
        ).hexdigest()[:16]
        edited = st.data_editor(
            display,
            hide_index=True,
            key=f'report_transactions_{filter_hash}',
            disabled=[column for column in display.columns if column != 'Сонгох'],
            column_config={
                'Сонгох': st.column_config.CheckboxColumn('Сонгох'),
                'id': st.column_config.NumberColumn('Дугаар', format='%d'),
                'date': 'Огноо',
                'direction': 'Чиглэл',
                'amount_display': 'Дүн',
                'merchant': 'Худалдаа, үйлчилгээ',
                'description': 'Гүйлгээний утга',
                'category': 'Ангилал',
                'classified_by': 'Ангилсан арга',
            },
        )
        selected_indices = edited.index[edited['Сонгох'].astype(bool)].tolist()
        selected_rows = rows.loc[selected_indices] if selected_indices else rows.iloc[0:0]
        selected_income = int(
            selected_rows.loc[selected_rows['amount'] > 0, 'amount'].sum()
        )
        selected_expense = int(
            -selected_rows.loc[selected_rows['amount'] < 0, 'amount'].sum()
        )
        result_cols = st.columns(4)
        result_cols[0].metric('Сонгосон гүйлгээ', f'{len(selected_rows):,}')
        result_cols[1].metric('Сонгосон орлого', _format_minor(selected_income))
        result_cols[2].metric('Сонгосон зарлага', _format_minor(selected_expense))
        result_cols[3].metric(
            'Сонгосон цэвэр дүн',
            _format_minor(selected_income - selected_expense),
        )

    if 'insights' in sections:
        _show_insights(
            monthly,
            spending,
            fees_minor,
            automatic,
            manual,
            unclassified,
            len(eligible),
        )
