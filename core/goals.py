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
        '''SELECT id, name, target_amount, current_savings, other_funding,
                  loan_amount, annual_interest_rate, loan_term_months,
                  loan_start_month, target_date
           FROM goals
           ORDER BY target_date, id''',
        conn,
    )
    columns = [
        'id',
        'name',
        'target_date',
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
            - int(row['current_savings'])
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
            'Хугацаа хэтэрсэн'
            if selected_index > target_index and remaining
            else 'Санхүүжилт бүрдсэн'
            if remaining == 0
            else 'Төлөвлөж байна'
        )
        plans.append(
            {
                'id': int(row['id']),
                'name': row['name'],
                'target_date': row['target_date'],
                'remaining_target_minor': remaining,
                'months_to_target': months,
                'monthly_saving_minor': saving,
                'monthly_loan_minor': loan_payment,
                'monthly_commitment_minor': saving + loan_payment,
                'status': status,
            }
        )
    return pd.DataFrame.from_records(plans, columns=columns)
