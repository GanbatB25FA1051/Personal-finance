"""Test long-term goal funding and loan estimates."""

from decimal import Decimal

import pytest

from core.db import get_conn, init_db
from core.goals import (
    delete_goal,
    goal_monthly_plan,
    list_goals,
    monthly_loan_payment,
    save_goal,
    record_goal_savings_movement, list_goal_savings_movements,
)


def _goal_connection():
    conn = get_conn(':memory:')
    init_db(conn)
    return conn


def test_zero_interest_loan_payment_is_principal_divided_by_term() -> None:
    assert monthly_loan_payment(1_200_000, Decimal('0'), 12) == 100_000


def test_interest_loan_payment_uses_amortized_fixed_payment() -> None:
    assert monthly_loan_payment(12_000_000, Decimal('12'), 12) == 1_066_185


def test_goal_plan_combines_savings_loan_and_other_funding() -> None:
    conn = _goal_connection()
    try:
        goal_id = save_goal(
            conn,
            'Орон сууц',
            10_000_000_000,
            200_000_000,
            50_000_000,
            6_000_000_000,
            Decimal('12'),
            12,
            '2026-11',
            '2027-07-28',
        )

        plan = goal_monthly_plan(conn, '2026-11').iloc[0]

        assert plan['id'] == goal_id
        assert plan['remaining_target_minor'] == 3_750_000_000
        assert plan['months_to_target'] == 9
        assert plan['monthly_saving_minor'] == 416_666_667
        assert plan['monthly_loan_minor'] == 533_092_732
        assert plan['monthly_commitment_minor'] == (
            416_666_667 + 533_092_732
        )
        assert plan['status'] == 'Төлөвлөж байна'
    finally:
        conn.close()


def test_multiple_goals_combine_funding_and_roll_into_monthly_budget_plan() -> None:
    conn = _goal_connection()
    try:
        home_id = save_goal(
            conn, 'Байр', 12_000_000_000, 2_000_000_000, 1_000_000_000,
            6_000_000_000, Decimal('0'), 12, '2026-11', '2027-10-31',
        )
        car_id = save_goal(
            conn, 'Машин', 5_000_000_000, 500_000_000, 500_000_000,
            2_000_000_000, Decimal('0'), 8, '2026-11', '2027-06-01',
        )

        plans = goal_monthly_plan(conn, '2026-11').set_index('id')
        assert plans.loc[home_id, 'monthly_saving_minor'] == 250_000_000
        assert plans.loc[home_id, 'monthly_loan_minor'] == 500_000_000
        assert plans.loc[car_id, 'monthly_saving_minor'] == 250_000_000
        assert plans.loc[car_id, 'monthly_loan_minor'] == 250_000_000
        assert plans['monthly_commitment_minor'].sum() == 1_250_000_000
        # Goal plans are visible to the budget, but do not create actual expenses.
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0
    finally:
        conn.close()


def test_loan_payment_is_limited_to_its_saved_term() -> None:
    conn = _goal_connection()
    try:
        save_goal(
            conn,
            'Машин',
            1_000_000_000,
            0,
            0,
            500_000_000,
            Decimal('0'),
            2,
            '2026-11',
            '2027-12-01',
        )

        assert goal_monthly_plan(conn, '2026-11').iloc[0][
            'monthly_loan_minor'
        ] == 250_000_000
        assert goal_monthly_plan(conn, '2027-01').iloc[0][
            'monthly_loan_minor'
        ] == 0
    finally:
        conn.close()


def test_goal_can_be_updated_and_deleted() -> None:
    conn = _goal_connection()
    try:
        goal_id = save_goal(
            conn,
            'Машин',
            5_000_000_000,
            1_000_000_000,
            0,
            0,
            Decimal('0'),
            0,
            '2026-11',
            '2027-12-01',
        )
        save_goal(
            conn,
            'Машин',
            6_000_000_000,
            1_000_000_000,
            500_000_000,
            0,
            Decimal('0'),
            0,
            '2026-11',
            '2028-01-01',
            goal_id,
        )
        assert list_goals(conn)[0]['target_amount'] == 6_000_000_000

        delete_goal(conn, goal_id)
        assert list_goals(conn) == []
    finally:
        conn.close()


@pytest.mark.parametrize(
    (
        'target_amount',
        'current_savings',
        'other_funding',
        'loan_amount',
        'interest',
        'term',
        'loan_start_month',
        'target_date',
        'message',
    ),
    [
        (0, 0, 0, 0, Decimal('0'), 0, '2026-11', '2027-01-01', 'greater than zero'),
        (100, 40, 40, 30, Decimal('0'), 0, '2026-11', '2027-01-01', 'cannot exceed'),
        (100, 0, 0, 50, Decimal('0'), 0, '2026-11', '2027-01-01', 'loan term'),
        (100, 0, 0, 50, Decimal('0'), 12, '2028-01', '2027-01-01', 'after the goal'),
        (100, 0, 0, 0, Decimal('101'), 0, '2026-11', '2027-01-01', 'interest rate'),
        (100, 0, 0, 0, Decimal('0'), 0, '2026-11', 'bad-date', 'target date'),
    ],
)
def test_invalid_goal_inputs_are_rejected(
    target_amount: int,
    current_savings: int,
    other_funding: int,
    loan_amount: int,
    interest: Decimal,
    term: int,
    loan_start_month: str,
    target_date: str,
    message: str,
) -> None:
    conn = _goal_connection()
    try:
        with pytest.raises(ValueError, match=message):
            save_goal(
                conn,
                'Зорилго',
                target_amount,
                current_savings,
                other_funding,
                loan_amount,
                interest,
                term,
                loan_start_month,
                target_date,
            )
        assert list_goals(conn) == []
    finally:
        conn.close()


def test_goal_past_target_is_marked_overdue() -> None:
    conn = _goal_connection()
    try:
        save_goal(
            conn,
            'Аялал',
            100_000,
            20_000,
            0,
            0,
            Decimal('0'),
            0,
            '2026-11',
            '2026-10-10',
        )
        plan = goal_monthly_plan(conn, '2026-11').iloc[0]
        assert plan['months_to_target'] == 0
        assert plan['monthly_saving_minor'] == 0
        assert plan['status'] == 'Хугацаа хэтэрсэн'
    finally:
        conn.close()


def test_savings_ledger_updates_progress_and_prevents_reusing_transfer() -> None:
    conn = _goal_connection()
    try:
        goal_id = save_goal(conn, 'Аялал', 1_000_000, 100_000, 0, 0,
                            Decimal('0'), 0, '2026-11', '2027-02-28')
        second_goal = save_goal(conn, 'Сургалт', 2_000_000, 0, 0, 0,
                                Decimal('0'), 0, '2026-11', '2027-12-01')
        transfer_category = conn.execute(
            "SELECT id FROM categories WHERE type='transfer' LIMIT 1"
        ).fetchone()[0]
        transaction_id = conn.execute(
            '''INSERT INTO transactions(txn_datetime, amount, direction, category_id)
               VALUES ('2026-12-05 10:00:00', -50000, 'out', ?) RETURNING id''',
            (transfer_category,),
        ).fetchone()[0]
        movement_id = record_goal_savings_movement(
            conn, goal_id, 50_000, '2026-12-05', 'Сарын нэмэлт хуримтлал', transaction_id,
        )
        plan = goal_monthly_plan(conn, '2026-12').set_index('id').loc[goal_id]
        assert movement_id > 0
        assert plan['current_savings_minor'] == 150_000
        assert plan['progress_percent'] == 15
        assert len(list_goal_savings_movements(conn, goal_id)) == 1
        with pytest.raises(ValueError, match='аль хэдийн холбосон'):
            record_goal_savings_movement(
                conn, second_goal, 50_000, '2026-12-05', '', transaction_id,
            )
        with pytest.raises(ValueError, match='0-ээс бага'):
            record_goal_savings_movement(conn, goal_id, -200_000, '2026-12-06')
        with pytest.raises(ValueError, match='түүхтэй зөрчилдөнө'):
            save_goal(conn, 'Аялал', 1_000_000, 960_000, 0, 0,
                      Decimal('0'), 0, '2026-11', '2027-02-28', goal_id)
    finally:
        conn.close()


def test_goal_status_completed_and_overdue_and_leap_day_target() -> None:
    conn = _goal_connection()
    try:
        complete_id = save_goal(conn, 'Дууссан', 100_000, 100_000, 0, 0,
                                Decimal('0'), 0, '2028-01', '2028-02-29')
        overdue_id = save_goal(conn, 'Хоцорсон', 100_000, 0, 0, 0,
                               Decimal('0'), 0, '2028-01', '2028-02-29')
        plans = goal_monthly_plan(conn, '2028-03').set_index('id')
        assert plans.loc[complete_id, 'status'] == 'Бүрэн санхүүжсэн'
        assert plans.loc[overdue_id, 'status'] == 'Хугацаа хэтэрсэн'
        assert plans.loc[overdue_id, 'months_to_target'] == 0
    finally:
        conn.close()
