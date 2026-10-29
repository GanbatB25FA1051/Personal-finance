"""Long-term purchase and savings goals."""

import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

import streamlit as st

from core.goals import (
    delete_goal,
    goal_monthly_plan,
    list_goals,
    save_goal,
    record_goal_savings_movement,
    list_goal_savings_movements,
)
from core.budget import next_month_start
from core.models import to_minor


def _format_minor(amount: int) -> str:
    """Format integer minor units as Mongolian tugrik."""
    units, cents = divmod(abs(int(amount)), 100)
    sign = '-' if amount < 0 else ''
    return f'{sign}{units:,}.{cents:02d} ₮'


def _amount_text(amount_minor: int) -> str:
    """Format an amount in minor units for a major-unit input."""
    units, cents = divmod(int(amount_minor), 100)
    return f'{units}.{cents:02d}'


def _parse_amount(value: str, label: str) -> int:
    """Parse a major-unit amount into integer minor units."""
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f'{label}: мөнгөн дүн буруу байна.') from exc
    if amount < 0:
        raise ValueError(f'{label}: сөрөг дүн оруулж болохгүй.')
    return to_minor(amount)


def _parse_rate(value: str) -> Decimal:
    """Parse and validate a non-negative annual loan interest rate."""
    try:
        rate = Decimal(value.strip() or '0')
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Жилийн хүүгийн хувь буруу байна.') from exc
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise ValueError('Жилийн хүү 0-100 хувийн хооронд байна.')
    return rate


def _goal_form(
    conn: sqlite3.Connection,
    goal: sqlite3.Row | None,
    form_key: str,
) -> None:
    """Render a form to create or update one goal."""
    with st.form(form_key):
        name = st.text_input(
            'Зорилгын нэр (жишээ: Байр, Машин)',
            value='' if goal is None else goal['name'],
        )
        left, right = st.columns(2)
        target = left.text_input(
            'Нийт үнэ / зорилтот дүн (₮)',
            value='' if goal is None else _amount_text(goal['target_amount']),
        )
        savings = right.text_input(
            'Эхлэх хуримтлал (₮)',
            value='0' if goal is None else _amount_text(goal['current_savings']),
        )
        other = left.text_input(
            'Гэр бүл / бусад санхүүжилт (₮)',
            value='0' if goal is None else _amount_text(goal['other_funding']),
        )
        loan = right.text_input(
            'Зээлийн үндсэн дүн (₮)',
            value='0' if goal is None else _amount_text(goal['loan_amount']),
        )
        annual_rate = left.text_input(
            'Зээлийн жилийн хүү (%)',
            value=(
                '0'
                if goal is None
                else str(goal['annual_interest_rate'])
            ),
            help='Зээлгүй бол 0 гэж үлдээнэ.',
        )
        loan_term = right.number_input(
            'Зээлийн хугацаа (сар)',
            min_value=0,
            max_value=600,
            step=1,
            value=0 if goal is None else int(goal['loan_term_months']),
        )
        loan_start = left.date_input(
            'Зээлийн төлөлт эхлэх сар',
            value=(
                next_month_start()
                if goal is None
                else date.fromisoformat(f"{goal['loan_start_month']}-01")
            ),
        )
        target_date = st.date_input(
            'Зорилгод хүрэх огноо',
            value=(
                date(date.today().year + 1, date.today().month, 1)
                if goal is None
                else date.fromisoformat(goal['target_date'])
            ),
        )
        submitted = st.form_submit_button(
            'Зорилго хадгалах' if goal is None else 'Өөрчлөлт хадгалах'
        )

    if not submitted:
        return
    try:
        target_minor = _parse_amount(target, 'Зорилтот дүн')
        savings_minor = _parse_amount(savings, 'Одоогийн хуримтлал')
        other_minor = _parse_amount(other, 'Бусад санхүүжилт')
        loan_minor = _parse_amount(loan, 'Зээлийн дүн')
        interest_rate = _parse_rate(annual_rate)
        goal_id = save_goal(
            conn,
            name,
            target_minor,
            savings_minor,
            other_minor,
            loan_minor,
            interest_rate,
            int(loan_term),
            loan_start.strftime('%Y-%m'),
            target_date.isoformat(),
            None if goal is None else int(goal['id']),
        )
    except ValueError as exc:
        st.error(str(exc))
        return
    st.success(f'Зорилго хадгалагдлаа. Дугаар: {goal_id}')
    st.rerun()


def render(conn: sqlite3.Connection) -> None:
    """Create goals and show their combined monthly funding plan."""
    st.title('Урт хугацааны санхүүгийн зорилго')
    st.caption(
        'Байр, машин зэрэг зорилгын нийт үнэ, өөрийн хуримтлал, зээл, '
        'гэр бүл/бусад санхүүжилтийг оруулж сар бүрийн хэрэгцээг тооцно. '
        'Энэ нь тооцоолол бөгөөд банкны зээлийн санал биш.'
    )
    existing = list_goals(conn)
    by_id = {int(row['id']): row for row in existing}
    if by_id:
        goal_labels = {
            f"{row['name']} · #{row['id']}": int(row['id'])
            for row in existing
        }
        selected_label = st.selectbox(
            'Зорилго сонгох',
            list(goal_labels),
            key='goal_selection',
        )
        selected_goal = by_id[goal_labels[selected_label]]
        with st.expander('Зорилгыг засах', expanded=False):
            _goal_form(
                conn,
                selected_goal,
                f"edit_goal_{selected_goal['id']}",
            )
        if st.button(
            'Сонгосон зорилгыг устгах',
            key=f"delete_goal_{selected_goal['id']}",
        ):
            delete_goal(conn, int(selected_goal['id']))
            st.success('Зорилго устгагдлаа.')
            st.rerun()

    with st.expander('Шинэ зорилго нэмэх', expanded=not bool(existing)):
        _goal_form(conn, None, 'create_goal')

    existing = list_goals(conn)
    if not existing:
        st.info('Зорилгын мэдээлэл нэмэхэд сар бүрийн төлөвлөгөө энд харагдана.')
        return

    selected_label = st.session_state.get('goal_selection')
    selected_id = next(
        (int(row['id']) for row in existing if f"{row['name']} · #{row['id']}" == selected_label),
        int(existing[0]['id']),
    )
    selected_goal = next(row for row in existing if int(row['id']) == selected_id)
    movements = list_goal_savings_movements(conn, selected_id)
    st.subheader(f"{selected_goal['name']} · хуримтлалын түүх")
    current_minor = int(selected_goal['current_savings']) + sum(int(row['amount_minor']) for row in movements)
    st.metric('Одоогийн хуримтлал', _format_minor(current_minor))
    st.caption('Хөдөлгөөн нь зөвхөн зорилгын хуримтлалын бүртгэлд орно; банкны бодит зарлага үүсгэхгүй.')
    with st.expander('Хуримтлалын хөдөлгөөн бүртгэх'):
        with st.form(f'goal_movement_form_{selected_id}'):
            movement_kind = st.selectbox('Хөдөлгөөний төрөл', ['Хуримтлал нэмэх', 'Хуримтлалаас хасах'], key=f'goal_movement_kind_{selected_id}')
            movement_amount = st.text_input('Дүн (₮)', key=f'goal_movement_amount_{selected_id}')
            movement_date = st.date_input('Огноо', value=date.today(), key=f'goal_movement_date_{selected_id}')
            movement_note = st.text_input('Тайлбар', key=f'goal_movement_note_{selected_id}')
            transfer_rows = conn.execute(
                '''SELECT t.id, t.txn_datetime, t.amount, t.direction, t.raw_description
                   FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
                   LEFT JOIN accounts a ON a.id=t.account_id
                   WHERE COALESCE(NULLIF(a.currency,''),'MNT')='MNT'
                     AND (c.type='transfer' OR EXISTS (
                       SELECT 1 FROM transfer_pairs tp WHERE tp.outgoing_transaction_id=t.id
                          OR tp.incoming_transaction_id=t.id
                   )) AND NOT EXISTS (
                       SELECT 1 FROM goal_savings_movements gm WHERE gm.source_transaction_id=t.id
                   ) ORDER BY t.txn_datetime DESC LIMIT 500'''
            ).fetchall()
            transfer_options = {'Гүйлгээ холбохгүй': None}
            for row in transfer_rows:
                label = f"{str(row['txn_datetime'])[:10]} · {_format_minor(abs(row['amount']))} · {str(row['raw_description'] or 'шилжүүлэг')[:40]} · #{row['id']}"
                transfer_options[label] = int(row['id'])
            transfer_label = st.selectbox('Холбогдох шилжүүлэг (заавал биш)', list(transfer_options), key=f'goal_movement_source_{selected_id}')
            movement_submitted = st.form_submit_button('Хөдөлгөөн хадгалах')
        if movement_submitted:
            try:
                amount_minor = to_minor(Decimal(movement_amount.strip()))
                if movement_kind == 'Хуримтлалаас хасах':
                    amount_minor = -amount_minor
                record_goal_savings_movement(
                    conn, selected_id, amount_minor, movement_date.isoformat(),
                    movement_note, transfer_options[transfer_label],
                )
                st.success('Хуримтлалын хөдөлгөөн бүртгэгдлээ.')
                st.rerun()
            except (ValueError, InvalidOperation) as exc:
                st.error(str(exc))
    if movements:
        movement_frame = pd.DataFrame([dict(row) for row in movements])
        movement_frame['Дүн'] = movement_frame['amount_minor'].map(_format_minor)
        movement_frame['Гүйлгээ'] = movement_frame['source_transaction_id'].map(
            lambda value: '—' if pd.isna(value) else f'#{int(value)}'
        )
        st.dataframe(
            movement_frame[['movement_date', 'Дүн', 'note', 'Гүйлгээ']],
            hide_index=True,
            column_config={
                'movement_date': 'Огноо', 'note': 'Тайлбар',
            },
        )
    else:
        st.info('Хуримтлалын хөдөлгөөний түүх хараахан алга.')

    month = st.date_input(
        'Төлөвлөгөөний төсвийн сар',
        value=next_month_start(),
        key='goal_budget_month',
    ).strftime('%Y-%m')
    plans = goal_monthly_plan(conn, month)
    if plans.empty:
        return

    total_savings = int(plans['monthly_saving_minor'].sum())
    total_loans = int(plans['monthly_loan_minor'].sum())
    total_commitment = int(plans['monthly_commitment_minor'].sum())
    cards = st.columns(3)
    cards[0].metric('Зорилгод хуримтлуулах', _format_minor(total_savings))
    cards[1].metric('Зээлийн сарын төлөлт', _format_minor(total_loans))
    cards[2].metric('Зорилгын сарын нийт дүн', _format_minor(total_commitment))
    st.caption(
        'Энэ сарын хуримтлуулах дүн болон зээлийн төлөлтийг Төсөв хэсгийн '
        'сарын төлөвлөгөөнд зорилгын тусгай мөрөөр нэгтгэнэ.'
    )

    display = plans.copy()
    display['target'] = display['target_date']
    display['remaining'] = display['remaining_target_minor'].map(_format_minor)
    display['progress'] = display['progress_percent'].map(lambda value: f'{value:,.1f}%')
    display['months'] = display['months_to_target'].map(
        lambda value: 'Хугацаа хэтэрсэн' if value == 0 else f'{value} сар'
    )
    display['saving'] = display['monthly_saving_minor'].map(_format_minor)
    display['loan'] = display['monthly_loan_minor'].map(_format_minor)
    display['commitment'] = display['monthly_commitment_minor'].map(_format_minor)
    st.dataframe(
        display[
            [
                'name',
                'target',
                'remaining',
                'progress',
                'months',
                'saving',
                'loan',
                'commitment',
                'status',
            ]
        ],
        hide_index=True,
        column_config={
            'name': 'Зорилго',
            'target': 'Хүрэх огноо',
            'remaining': 'Бүрдүүлэх дүн',
            'progress': 'Явц',
            'months': 'Үлдсэн хугацаа',
            'saving': 'Сарын хуримтлал',
            'loan': 'Сарын зээл',
            'commitment': 'Сарын нийт төлөвлөгөө',
            'status': 'Төлөв',
        },
    )
    chart = plans.set_index('name')[
        ['monthly_saving_minor', 'monthly_loan_minor']
    ].rename(
        columns={
            'monthly_saving_minor': 'Сарын хуримтлал',
            'monthly_loan_minor': 'Зээлийн төлөлт',
        }
    ) / 100
    st.bar_chart(chart)
