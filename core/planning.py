"""Recurring commitments and user-confirmed transaction reconciliation."""

import calendar
import sqlite3
from datetime import date, datetime
from typing import Any



def _parse_month(value: str) -> date:
    try:
        return date.fromisoformat(f'{value}-01')
    except ValueError as exc:
        raise ValueError('Сар YYYY-MM хэлбэртэй байна.') from exc


def _validate_payment_scope(
    conn: sqlite3.Connection,
    amount_minor: int,
    currency: str,
    account_id: int | None,
    category_id: int | None,
) -> str:
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise ValueError('Төлөвлөсөн дүн 0-ээс их бүхэл minor unit байна.')
    selected_currency = currency.strip().upper()
    if not selected_currency or len(selected_currency) > 8:
        raise ValueError('Валютын код буруу байна.')
    if account_id is not None:
        account = conn.execute(
            "SELECT COALESCE(NULLIF(currency,''),'MNT') AS currency FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if account is None or str(account['currency']).upper() != selected_currency:
            raise ValueError('Сонгосон дансны валют таарахгүй байна.')
    if category_id is not None:
        category = conn.execute(
            'SELECT type, is_active FROM categories WHERE id=?', (category_id,),
        ).fetchone()
        if category is None or not category['is_active'] or category['type'] not in ('expense', 'fee'):
            raise ValueError('Зөвхөн идэвхтэй зарлага эсвэл шимтгэлийн ангилал сонгоно.')
    return selected_currency


def save_recurring_payment(
    conn: sqlite3.Connection,
    name: str,
    amount_minor: int,
    currency: str,
    due_day: int,
    start_month: str,
    end_month: str | None = None,
    account_id: int | None = None,
    category_id: int | None = None,
    payment_id: int | None = None,
) -> int:
    """Create/update a monthly plan item; no actual expense is written."""
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError('Төлөвлөгөөний нэр хоосон байж болохгүй.')
    if isinstance(due_day, bool) or not isinstance(due_day, int) or not 1 <= due_day <= 31:
        raise ValueError('Төлөх өдөр 1-31 байна.')
    start = _parse_month(start_month)
    end = _parse_month(end_month) if end_month else None
    if end and end < start:
        raise ValueError('Дуусах сар эхлэх сараас өмнө байж болохгүй.')
    selected_currency = _validate_payment_scope(
        conn, amount_minor, currency, account_id, category_id,
    )
    now = datetime.now().isoformat(timespec='seconds')
    with conn:
        if payment_id is None:
            row = conn.execute(
                '''INSERT INTO recurring_payments(
                    name, amount_minor, currency, account_id, category_id, due_day,
                    start_month, end_month, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) RETURNING id''',
                (cleaned_name, amount_minor, selected_currency, account_id, category_id,
                 due_day, start_month, end_month, now, now),
            ).fetchone()
            return int(row['id'])
        cursor = conn.execute(
            '''UPDATE recurring_payments
               SET name=?, amount_minor=?, currency=?, account_id=?, category_id=?,
                   due_day=?, start_month=?, end_month=?, updated_at=?
               WHERE id=?''',
            (cleaned_name, amount_minor, selected_currency, account_id, category_id,
             due_day, start_month, end_month, now, payment_id),
        )
        if cursor.rowcount != 1:
            raise ValueError('Төлөвлөгөө олдсонгүй.')
    return payment_id


def list_recurring_payments(conn: sqlite3.Connection) -> list[Any]:
    return conn.execute(
        '''SELECT id, name, amount_minor, currency, account_id, category_id,
                  due_day, start_month, end_month, is_active
           FROM recurring_payments ORDER BY is_active DESC, name, id'''
    ).fetchall()


def set_recurring_payment_active(conn: sqlite3.Connection, payment_id: int, active: bool) -> None:
    with conn:
        cursor = conn.execute(
            'UPDATE recurring_payments SET is_active=?, updated_at=? WHERE id=?',
            (int(active), datetime.now().isoformat(timespec='seconds'), payment_id),
        )
        if cursor.rowcount != 1:
            raise ValueError('Төлөвлөгөө олдсонгүй.')


def planned_payments_for_month(
    conn: sqlite3.Connection,
    budget_month: str,
    account_id: int | None = None,
    currency: str = 'MNT',
) -> list[dict[str, Any]]:
    """Return this month's planned occurrences and confirmed actual matches."""
    selected = _parse_month(budget_month)
    month = f'{selected.year:04d}-{selected.month:02d}'
    account_sql = '1=1' if account_id is None else '(rp.account_id IS NULL OR rp.account_id=?)'
    params: list[Any] = [month, (currency or 'MNT').upper()]
    if account_id is not None:
        params.append(account_id)
    rows = conn.execute(
        f'''SELECT rp.id, rp.name, rp.amount_minor, rp.currency, rp.account_id,
                   rp.category_id, rp.due_day, rpm.transaction_id,
                   t.txn_datetime AS matched_date, t.amount AS matched_amount_minor
            FROM recurring_payments rp
            LEFT JOIN recurring_payment_matches rpm
              ON rpm.payment_id=rp.id AND rpm.occurrence_month=?
            LEFT JOIN transactions t ON t.id=rpm.transaction_id
            WHERE rp.is_active=1 AND rp.currency=? AND {account_sql}
              AND rp.start_month<=? AND (rp.end_month IS NULL OR rp.end_month>=?)
            ORDER BY rp.due_day, rp.name, rp.id''',
        [*params, month, month],
    ).fetchall()
    result: list[dict[str, Any]] = []
    last_day = calendar.monthrange(selected.year, selected.month)[1]
    due_date = lambda day: date(selected.year, selected.month, min(int(day), last_day)).isoformat()
    for row in rows:
        item = dict(row)
        item['occurrence_month'] = month
        item['due_date'] = due_date(item['due_day'])
        item['is_matched'] = item['transaction_id'] is not None
        item['unmatched_planned_minor'] = 0 if item['is_matched'] else int(item['amount_minor'])
        result.append(item)
    return result


def available_actual_expenses(
    conn: sqlite3.Connection,
    occurrence_month: str,
    account_id: int | None,
    currency: str,
) -> list[Any]:
    """Return unassigned actual expenses in the plan's month and currency."""
    month = _parse_month(occurrence_month)
    end_day = calendar.monthrange(month.year, month.month)[1]
    clauses = [
        'substr(t.txn_datetime,1,10)>=?', 'substr(t.txn_datetime,1,10)<=?',
        't.amount<0', "(c.type IS NULL OR c.type IN ('expense','fee'))",
        "NOT EXISTS (SELECT 1 FROM transfer_pairs tp WHERE tp.outgoing_transaction_id=t.id OR tp.incoming_transaction_id=t.id)",
        "COALESCE(NULLIF(a.currency, ''), 'MNT')=?",
        'NOT EXISTS (SELECT 1 FROM recurring_payment_matches m WHERE m.transaction_id=t.id)',
    ]
    params: list[Any] = [month.isoformat(), date(month.year, month.month, end_day).isoformat(), currency.upper()]
    if account_id is not None:
        clauses.append('t.account_id=?')
        params.append(account_id)
    return conn.execute(
        f'''SELECT t.id, t.txn_datetime, t.amount, t.raw_description,
                   t.merchant_clean, t.account_id
            FROM transactions t
            LEFT JOIN categories c ON c.id=t.category_id
            LEFT JOIN accounts a ON a.id=t.account_id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.txn_datetime, t.id''',
        params,
    ).fetchall()


def confirm_planned_payment_match(
    conn: sqlite3.Connection,
    payment_id: int,
    occurrence_month: str,
    transaction_id: int,
) -> None:
    """User-confirm one actual expense for a planned occurrence."""
    occurrence = _parse_month(occurrence_month)
    payment = conn.execute(
        'SELECT currency, account_id, start_month, end_month, is_active FROM recurring_payments WHERE id=?',
        (payment_id,),
    ).fetchone()
    if payment is None or not payment['is_active']:
        raise ValueError('Идэвхтэй төлөвлөгөө олдсонгүй.')
    if occurrence_month < payment['start_month'] or (payment['end_month'] and occurrence_month > payment['end_month']):
        raise ValueError('Сонгосон сар төлөвлөгөөний хугацаанд багтахгүй.')
    already_used = conn.execute(
        'SELECT payment_id, occurrence_month FROM recurring_payment_matches WHERE transaction_id=?',
        (transaction_id,),
    ).fetchone()
    if already_used:
        raise ValueError('Энэ гүйлгээ өөр төлөвлөгөөтэй аль хэдийн холбогдсон байна.')
    existing_occurrence = conn.execute(
        'SELECT 1 FROM recurring_payment_matches WHERE payment_id=? AND occurrence_month=?',
        (payment_id, f'{occurrence.year:04d}-{occurrence.month:02d}'),
    ).fetchone()
    if existing_occurrence:
        raise ValueError('Энэ төлөвлөгөөний сард гүйлгээ аль хэдийн холбосон байна.')
    candidates = available_actual_expenses(
        conn, occurrence_month, payment['account_id'], payment['currency'],
    )
    if int(transaction_id) not in {int(row['id']) for row in candidates}:
        raise ValueError('Гүйлгээ валют, огноо эсвэл төлөвлөгөөний scope-д тохирохгүй байна.')
    try:
        with conn:
            conn.execute(
                '''INSERT INTO recurring_payment_matches(
                       payment_id, occurrence_month, transaction_id, confirmed_at
                   ) VALUES (?, ?, ?, ?)''',
                (payment_id, f'{occurrence.year:04d}-{occurrence.month:02d}', transaction_id,
                 datetime.now().isoformat(timespec='seconds')),
            )
    except Exception as exc:
        if type(exc).__name__ in ('IntegrityError', 'UniqueViolation'):
            raise ValueError('Энэ төлөвлөгөөний сар эсвэл гүйлгээ аль хэдийн холбогдсон байна.') from exc
        raise


def remove_planned_payment_match(
    conn: sqlite3.Connection, payment_id: int, occurrence_month: str,
) -> None:
    month = _parse_month(occurrence_month)
    with conn:
        conn.execute(
            'DELETE FROM recurring_payment_matches WHERE payment_id=? AND occurrence_month=?',
            (payment_id, f'{month.year:04d}-{month.month:02d}'),
        )
