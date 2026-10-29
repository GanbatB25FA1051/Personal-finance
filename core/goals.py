"""Long-term financial goals and monthly funding plans."""

import sqlite3
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP
from typing import Any

import pandas as pd

from core.db import read_sql_query


def _validate_goal(
    name: str,
    target_amount: int,
    current_savings: int,
    other_funding: int,
    loan_amount: int,
    annual_interest_rate: Decimal,
    loan_term_months: int,
    loan_start_month: str,
    target_date: str,
) -> None:
    """Validate all saved goal inputs before changing the database."""
    if not name.strip():
        raise ValueError('Goal name is required.')
    if any(
        isinstance(amount, bool) or not isinstance(amount, int) or amount < 0
        for amount in (
            target_amount,
            current_savings,
            other_funding,
            loan_amount,
        )
    ):
        raise ValueError('Goal amounts must be non-negative integer minor units.')
    if target_amount <= 0:
        raise ValueError('Goal target amount must be greater than zero.')
    if current_savings + other_funding + loan_amount > target_amount:
        raise ValueError('Funding sources cannot exceed the goal target amount.')
    if (
        not annual_interest_rate.is_finite()
        or annual_interest_rate < 0
        or annual_interest_rate > 100
    ):
        raise ValueError('Annual interest rate must be between 0 and 100 percent.')
    if loan_amount > 0 and loan_term_months <= 0:
        raise ValueError('A loan term is required when a loan is included.')
    if loan_amount == 0 and loan_term_months < 0:
        raise ValueError('Loan term cannot be negative.')
    try:
        date.fromisoformat(target_date)
    except ValueError as exc:
        raise ValueError('Goal target date must use YYYY-MM-DD format.') from exc
    try:
        start_month = date.fromisoformat(f'{loan_start_month}-01')
    except ValueError as exc:
        raise ValueError('Loan start month must use YYYY-MM format.') from exc
    target_month = date.fromisoformat(f'{target_date[:7]}-01')
    if loan_amount > 0 and start_month > target_month:
        raise ValueError('Loan start month cannot be after the goal target month.')


def list_goals(conn: sqlite3.Connection) -> list[Any]:
    """Return saved goals ordered by target date."""
    return conn.execute(
        '''SELECT id, name, target_amount, current_savings, other_funding,
                  loan_amount, annual_interest_rate, loan_term_months,
                  loan_start_month, target_date
           FROM goals
           ORDER BY target_date, id'''
    ).fetchall()


def save_goal(
    conn: sqlite3.Connection,
    name: str,
    target_amount: int,
    current_savings: int,
    other_funding: int,
    loan_amount: int,
    annual_interest_rate: Decimal,
    loan_term_months: int,
    loan_start_month: str,
    target_date: str,
    goal_id: int | None = None,
) -> int:
    """Create or update a goal using integer minor units for money."""
    cleaned_name = name.strip()
    _validate_goal(
        cleaned_name,
        target_amount,
        current_savings,
        other_funding,
        loan_amount,
        annual_interest_rate,
        loan_term_months,
        loan_start_month,
        target_date,
    )
    if goal_id is not None:
        movement_total = conn.execute(
            'SELECT COALESCE(SUM(amount_minor),0) FROM goal_savings_movements WHERE goal_id=?',
            (goal_id,),
        ).fetchone()[0]
        effective_savings = current_savings + int(movement_total)
        if effective_savings < 0 or effective_savings + other_funding + loan_amount > target_amount:
            raise ValueError('Эхлэх хуримтлалын өөрчлөлт хадгалсан хөдөлгөөний түүхтэй зөрчилдөнө.')
    updated_at = datetime.now().isoformat(timespec='seconds')
    with conn:
        if goal_id is None:
            row = conn.execute(
                '''INSERT INTO goals(
                       name, target_amount, current_savings, other_funding,
                       loan_amount, annual_interest_rate, loan_term_months,
                       loan_start_month, target_date, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   RETURNING id''',
                (
                    cleaned_name,
                    target_amount,
                    current_savings,
                    other_funding,
                    loan_amount,
                    str(annual_interest_rate),
                    loan_term_months,
                    loan_start_month,
                    target_date,
                    updated_at,
                    updated_at,
                ),
            ).fetchone()
            return int(row['id'])

        cursor = conn.execute(
            '''UPDATE goals
               SET name=?, target_amount=?, current_savings=?, other_funding=?,
                   loan_amount=?, annual_interest_rate=?, loan_term_months=?,
                   loan_start_month=?, target_date=?, updated_at=?
               WHERE id=?''',
            (
                cleaned_name,
                target_amount,
                current_savings,
                other_funding,
                loan_amount,
                str(annual_interest_rate),
                loan_term_months,
                loan_start_month,
                target_date,
                updated_at,
                goal_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError('Goal was not found.')
    return goal_id


def delete_goal(conn: sqlite3.Connection, goal_id: int) -> None:
    """Delete one saved goal."""
    with conn:
        cursor = conn.execute('DELETE FROM goals WHERE id=?', (goal_id,))
        if cursor.rowcount != 1:
            raise ValueError('Goal was not found.')



def record_goal_savings_movement(
    conn: sqlite3.Connection,
    goal_id: int,
    amount_minor: int,
    movement_date: str,
    note: str = '',
    source_transaction_id: int | None = None,
) -> int:
    """Record one signed savings movement without creating a transaction."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor == 0:
        raise ValueError('Хуримтлалын хөдөлгөөн 0-ээс ялгаатай бүхэл дүн байна.')
    try:
        date.fromisoformat(movement_date)
    except ValueError as exc:
        raise ValueError('Хөдөлгөөний огноо YYYY-MM-DD байна.') from exc
    goal = conn.execute(
        'SELECT target_amount, current_savings, other_funding, loan_amount FROM goals WHERE id=?',
        (goal_id,),
    ).fetchone()
    if goal is None:
        raise ValueError('Зорилго олдсонгүй.')
    movement_total = conn.execute(
        'SELECT COALESCE(SUM(amount_minor),0) FROM goal_savings_movements WHERE goal_id=?',
        (goal_id,),
    ).fetchone()[0]
    new_savings = int(goal['current_savings']) + int(movement_total) + amount_minor
    if new_savings < 0:
        raise ValueError('Хуримтлалын үлдэгдэл 0-ээс бага болохгүй.')
    if new_savings + int(goal['other_funding']) + int(goal['loan_amount']) > int(goal['target_amount']):
        raise ValueError('Хөдөлгөөн зорилгын санхүүжилтийн дээд дүнг давуулна.')
    if source_transaction_id is not None:
        source = conn.execute(
            '''SELECT t.id, t.amount, t.direction, c.type,
                      COALESCE(NULLIF(a.currency,''),'MNT') AS currency,
                      EXISTS (SELECT 1 FROM transfer_pairs tp
                              WHERE tp.outgoing_transaction_id=t.id
                                 OR tp.incoming_transaction_id=t.id) AS paired
               FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
               LEFT JOIN accounts a ON a.id=t.account_id
               WHERE t.id=?''',
            (source_transaction_id,),
        ).fetchone()
        if source is None:
            raise ValueError('Холбох гүйлгээ олдсонгүй.')
        if str(source['currency']).upper() != 'MNT':
            raise ValueError('Зорилгын хуримтлал MNT байна; өөр валютын гүйлгээг холбохгүй.')
        if source['type'] != 'transfer' and not source['paired']:
            raise ValueError('Зөвхөн шилжүүлгийн гүйлгээг хуримтлалын хөдөлгөөнтэй холбож болно.')
        if (amount_minor > 0 and source['direction'] != 'out') or (amount_minor < 0 and source['direction'] != 'in'):
            raise ValueError('Хөдөлгөөний чиглэл холбосон шилжүүлэгтэй таарахгүй байна.')
        if abs(amount_minor) != abs(int(source['amount'])):
            raise ValueError('Хөдөлгөөний дүн холбосон шилжүүлгийн дүнтэй таарахгүй байна.')
    try:
        with conn:
            row = conn.execute(
                '''INSERT INTO goal_savings_movements(
                       goal_id, movement_date, amount_minor, note,
                       source_transaction_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?) RETURNING id''',
                (goal_id, movement_date, amount_minor, note.strip(), source_transaction_id,
                 datetime.now().isoformat(timespec='seconds')),
            ).fetchone()
    except Exception as exc:
        if type(exc).__name__ in ('IntegrityError', 'UniqueViolation'):
            raise ValueError('Энэ гүйлгээг өөр хуримтлалд аль хэдийн холбосон байна.') from exc
        raise
    return int(row['id'])


def list_goal_savings_movements(conn: sqlite3.Connection, goal_id: int) -> list[Any]:
    """Return a goal's savings ledger in chronological order."""
    return conn.execute(
        '''SELECT id, goal_id, movement_date, amount_minor, note,
                  source_transaction_id, created_at
           FROM goal_savings_movements WHERE goal_id=?
           ORDER BY movement_date, id''',
        (goal_id,),
    ).fetchall()


def monthly_loan_payment(
    principal_minor: int,
    annual_interest_rate: Decimal,
    term_months: int,
) -> int:
    """Calculate a fixed monthly loan installment in minor units."""
    if principal_minor <= 0:
        return 0
    if term_months <= 0:
        raise ValueError('Loan term must be greater than zero.')
    monthly_rate = annual_interest_rate / Decimal(1200)
    if monthly_rate == 0:
        payment = Decimal(principal_minor) / Decimal(term_months)
    else:
        factor = (Decimal(1) + monthly_rate) ** term_months
        payment = (
            Decimal(principal_minor)
            * monthly_rate
            * factor
            / (factor - Decimal(1))
        )
    return int(payment.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def goal_monthly_plan(
    conn: sqlite3.Connection,
    budget_month: str,
) -> pd.DataFrame:
    """Return each goal's required savings and loan installment for a month."""
    try:
        selected_month = date.fromisoformat(f'{budget_month}-01')
    except ValueError as exc:
        raise ValueError('Budget month must use YYYY-MM format.') from exc

    goals = read_sql_query(
        '''SELECT g.id, g.name, g.target_amount, g.current_savings,
                  g.current_savings + COALESCE(m.saved_minor,0) AS effective_savings_minor,
                  g.other_funding, g.loan_amount, g.annual_interest_rate,
                  g.loan_term_months, g.loan_start_month, g.target_date
           FROM goals AS g
           LEFT JOIN (
               SELECT goal_id, SUM(amount_minor) AS saved_minor
               FROM goal_savings_movements GROUP BY goal_id
           ) AS m ON m.goal_id=g.id
           ORDER BY g.target_date, g.id''',
        conn,
    )
    columns = [
        'id',
        'name',
        'target_date',
        'current_savings_minor',
        'funded_minor',
        'progress_percent',
        'remaining_target_minor',
        'months_to_target',
        'monthly_saving_minor',
        'monthly_loan_minor',
        'monthly_commitment_minor',
        'status',
    ]
    if goals.empty:
        return pd.DataFrame(columns=columns)

    plans: list[dict[str, Any]] = []
    selected_index = selected_month.year * 12 + selected_month.month
    for row in goals.to_dict('records'):
        target = date.fromisoformat(row['target_date'])
        target_index = target.year * 12 + target.month
        remaining = max(
            0,
            int(row['target_amount'])
            - int(row['effective_savings_minor'])
            - int(row['other_funding'])
            - int(row['loan_amount']),
        )
        months = (
            max(1, target_index - selected_index + 1)
            if selected_index <= target_index
            else 0
        )
        saving = (
            int(
                (Decimal(remaining) / Decimal(months)).quantize(
                    Decimal('1'),
                    rounding=ROUND_UP,
                )
            )
            if months and remaining
            else 0
        )
        loan_months = int(row['loan_term_months'])
        loan_payment = 0
        if int(row['loan_amount']) and loan_months:
            loan_start = date.fromisoformat(
                f"{row['loan_start_month']}-01"
            )
            loan_start_index = loan_start.year * 12 + loan_start.month
            loan_payment_index = selected_index - loan_start_index
            if 0 <= loan_payment_index < loan_months:
                loan_payment = monthly_loan_payment(
                    int(row['loan_amount']),
                    Decimal(str(row['annual_interest_rate'])),
                    loan_months,
                )
        status = (
            'Бүрэн санхүүжсэн'
            if remaining == 0
            else 'Хугацаа хэтэрсэн'
            if selected_index > target_index
            else 'Төлөвлөж байна'
        )
        plans.append(
            {
                'id': int(row['id']),
                'name': row['name'],
                'target_date': row['target_date'],
                'current_savings_minor': int(row['effective_savings_minor']),
                'funded_minor': (
                    int(row['effective_savings_minor'])
                    + int(row['other_funding']) + int(row['loan_amount'])
                ),
                'progress_percent': (
                    min(100.0, (
                        int(row['effective_savings_minor'])
                        + int(row['other_funding']) + int(row['loan_amount'])
                    ) / int(row['target_amount']) * 100)
                ),
                'remaining_target_minor': remaining,
                'months_to_target': months,
                'monthly_saving_minor': saving,
                'monthly_loan_minor': loan_payment,
                'monthly_commitment_minor': saving + loan_payment,
                'status': status,
            }
        )
    return pd.DataFrame.from_records(plans, columns=columns)
