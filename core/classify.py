"""Rule matching and manual transaction classification."""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator


@contextmanager
def _atomic(conn: Any) -> Iterator[None]:
    owns_transaction = not bool(getattr(conn, 'in_transaction', False))
    if owns_transaction:
        conn.execute('BEGIN')
    conn.execute('SAVEPOINT classification_operation')
    try:
        yield
        conn.execute('RELEASE SAVEPOINT classification_operation')
        if owns_transaction:
            conn.commit()
    except Exception as original_error:
        savepoint_recovered = False
        try:
            conn.execute('ROLLBACK TO SAVEPOINT classification_operation')
            conn.execute('RELEASE SAVEPOINT classification_operation')
            savepoint_recovered = True
        except Exception:
            pass
        if owns_transaction or not savepoint_recovered:
            try:
                conn.rollback()
            except Exception:
                pass
        raise original_error


def _audit(conn: Any, entity_type: str, entity_id: int, action: str,
           old_values: dict | None, new_values: dict | None) -> None:
    conn.execute(
        "INSERT INTO classification_audit "
        "(entity_type, entity_id, action, old_values, new_values, changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (entity_type, entity_id, action,
         json.dumps(old_values, ensure_ascii=False, sort_keys=True),
         json.dumps(new_values, ensure_ascii=False, sort_keys=True),
         datetime.now().isoformat(timespec='seconds')),
    )


def _category_supports(kind: str, direction: str) -> bool:
    return kind == 'transfer' or (kind == 'income' and direction == 'in') or (
        kind in ('expense', 'fee') and direction == 'out'
    )


def _category(conn: Any, category_id: int, direction: str | None = None,
              *, active: bool = True) -> Any:
    row = conn.execute(
        'SELECT id, name, type, is_active FROM categories WHERE id=?',
        (category_id,),
    ).fetchone()
    if row is None:
        raise ValueError('Category does not exist')
    if active and not row['is_active']:
        raise ValueError('Category is archived')
    if direction is not None and not _category_supports(row['type'], direction):
        raise ValueError('Category is incompatible with transaction direction')
    return row


def _rule_category_valid(conn: Any, category_id: int,
                         direction_filter: str) -> Any:
    category = _category(conn, category_id)
    directions = ('in', 'out') if direction_filter == 'any' else (direction_filter,)
    if not all(_category_supports(category['type'], direction) for direction in directions):
        raise ValueError('Category is incompatible with rule direction')
    return category


def add_rule(conn: sqlite3.Connection, match_type: str, pattern: str,
             category_id: int, direction_filter: str = 'any',
             priority: int = 100) -> int:
    if match_type not in {'counterparty_account', 'merchant_exact', 'contains', 'regex'}:
        raise ValueError('Invalid match type')
    if direction_filter not in {'in', 'out', 'any'}:
        raise ValueError('Invalid direction filter')
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError('Pattern must be non-empty text')
    pattern = pattern.strip()
    _rule_category_valid(conn, category_id, direction_filter)
    if match_type == 'regex':
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f'Invalid regex: {exc}') from exc
    with _atomic(conn):
        _rule_category_valid(conn, category_id, direction_filter)
        cursor = conn.execute(
            '''INSERT INTO merchant_rules
               (match_type, pattern, category_id, direction_filter, priority, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (match_type, pattern, category_id, direction_filter, priority,
             datetime.now().isoformat()),
        )
        rule_id = int(cursor.lastrowid)
        _audit(conn, 'rule', rule_id, 'create', None, {
            'match_type': match_type, 'pattern': pattern,
            'category_id': category_id, 'direction_filter': direction_filter,
            'priority': priority, 'is_active': 1,
        })
    return rule_id


def update_rule(conn: Any, rule_id: int, *, category_id: int | None = None,
                priority: int | None = None, is_active: bool | None = None) -> int:
    with _atomic(conn):
        row = conn.execute('SELECT * FROM merchant_rules WHERE id=?', (rule_id,)).fetchone()
        if row is None:
            raise ValueError('Rule does not exist')
        category_id = row['category_id'] if category_id is None else category_id
        priority = row['priority'] if priority is None else int(priority)
        is_active = bool(row['is_active']) if is_active is None else bool(is_active)
        if is_active or category_id != row['category_id']:
            _rule_category_valid(conn, category_id, row['direction_filter'])
        old = {'category_id': row['category_id'], 'priority': row['priority'],
               'is_active': bool(row['is_active'])}
        new = {'category_id': category_id, 'priority': priority, 'is_active': is_active}
        if old == new:
            return 0
        conn.execute('UPDATE merchant_rules SET category_id=?, priority=?, is_active=? WHERE id=?',
                     (category_id, priority, int(is_active), rule_id))
        _audit(conn, 'rule', rule_id, 'update', old, new)
        changed_transactions = 0
        if old['category_id'] != category_id:
            rows = conn.execute("SELECT id, category_id, rule_id, classified_by FROM transactions WHERE rule_id=? AND classified_by='rule'", (rule_id,)).fetchall()
            for txn in rows:
                conn.execute('UPDATE transactions SET category_id=? WHERE id=?', (category_id, txn['id']))
                _audit(conn, 'transaction', txn['id'], 'rule_category_change',
                       {'category_id': txn['category_id'], 'rule_id': rule_id,
                        'classified_by': txn['classified_by']},
                       {'category_id': category_id, 'rule_id': rule_id,
                        'classified_by': txn['classified_by']})
                changed_transactions += 1
        return changed_transactions


def reset_automatic_classification(conn: Any, txn_ids: list[int],
                                   sources: tuple[str, ...] = ('none', 'system', 'rule')) -> int:
    ids = list(dict.fromkeys(txn_ids))
    if not ids:
        return 0
    source_placeholders = ','.join('?' for _ in sources)
    changed = 0
    with _atomic(conn):
        for offset in range(0, len(ids), 900):
            batch = ids[offset:offset + 900]
            placeholders = ','.join('?' for _ in batch)
            rows = conn.execute(
                f"SELECT id, category_id, rule_id, classified_by FROM transactions WHERE id IN ({placeholders}) AND classified_by IN ({source_placeholders})",
                (*batch, *sources),
            ).fetchall()
            for row in rows:
                conn.execute("UPDATE transactions SET category_id=NULL, rule_id=NULL, classified_by='none' WHERE id=?", (row['id'],))
                _audit(conn, 'transaction', row['id'], 'reset_automatic_classification',
                       {'category_id': row['category_id'], 'rule_id': row['rule_id'],
                        'classified_by': row['classified_by']},
                       {'category_id': None, 'rule_id': None, 'classified_by': 'none'})
                changed += 1
    return changed



def set_category_active(conn: Any, category_id: int, active: bool) -> None:
    with _atomic(conn):
        row = conn.execute('SELECT id, name, is_active FROM categories WHERE id=?',
                           (category_id,)).fetchone()
        if row is None:
            raise ValueError('Category does not exist')
        old_active = bool(row['is_active'])
        active = bool(active)
        if old_active == active:
            return
        conn.execute('UPDATE categories SET is_active=? WHERE id=?',
                     (int(active), category_id))
        _audit(conn, 'category', category_id, 'activate' if active else 'archive',
               {'name': row['name'], 'is_active': old_active},
               {'name': row['name'], 'is_active': active})
        if not active:
            rule_ids = [rule['id'] for rule in conn.execute(
                'SELECT id FROM merchant_rules WHERE category_id=? AND is_active=1',
                (category_id,),
            ).fetchall()]
            for rule_id in rule_ids:
                update_rule(conn, rule_id, is_active=False)


def delete_rule(conn: Any, rule_id: int) -> None:
    with _atomic(conn):
        row = conn.execute('SELECT * FROM merchant_rules WHERE id=?', (rule_id,)).fetchone()
        if row is None:
            return
        linked = conn.execute('SELECT id, category_id, rule_id, classified_by FROM transactions WHERE rule_id=?', (rule_id,)).fetchall()
        for txn in linked:
            conn.execute('UPDATE transactions SET rule_id=NULL WHERE id=?', (txn['id'],))
            _audit(conn, 'transaction', txn['id'], 'detach_rule',
                   {'category_id': txn['category_id'], 'rule_id': txn['rule_id'],
                    'classified_by': txn['classified_by']},
                   {'category_id': txn['category_id'], 'rule_id': None,
                    'classified_by': txn['classified_by']})
        _audit(conn, 'rule', rule_id, 'delete',
               {'match_type': row['match_type'], 'pattern': row['pattern'],
                'category_id': row['category_id'], 'direction_filter': row['direction_filter'],
                'priority': row['priority'], 'is_active': bool(row['is_active'])}, None)
        conn.execute('DELETE FROM merchant_rules WHERE id=?', (rule_id,))


def reapply_rules(conn: Any) -> int:
    with _atomic(conn):
        rows = conn.execute("SELECT id, category_id, rule_id, classified_by FROM transactions WHERE classified_by='rule'").fetchall()
        for row in rows:
            conn.execute("UPDATE transactions SET category_id=NULL, rule_id=NULL, classified_by='none' WHERE id=? AND classified_by='rule'", (row['id'],))
            _audit(conn, 'transaction', row['id'], 'reset_rule_classification',
                   {'category_id': row['category_id'], 'rule_id': row['rule_id'],
                    'classified_by': row['classified_by']},
                   {'category_id': None, 'rule_id': None, 'classified_by': 'none'})
        return apply_rules(conn)


def find_rule(conn: sqlite3.Connection, txn: sqlite3.Row) -> sqlite3.Row | None:
    """Return the first active rule matching a transaction."""
    raw_description = txn['raw_description'] or ''
    direction = txn['direction']

    for rule in conn.execute(
        '''SELECT r.* FROM merchant_rules AS r
           JOIN categories AS c ON c.id=r.category_id
           WHERE r.is_active=1 AND c.is_active=1
           ORDER BY r.priority ASC, r.id ASC'''
    ):
        if rule['direction_filter'] not in ('any', direction):
            continue
        category = conn.execute('SELECT type FROM categories WHERE id=? AND is_active=1',
                                (rule['category_id'],)).fetchone()
        if category is None or not _category_supports(category['type'], direction):
            continue

        pattern = rule['pattern']
        if not isinstance(pattern, str):
            continue

        match_type = rule['match_type']
        if match_type == 'counterparty_account':
            value = txn['counterparty_account']
            matched = bool(value) and value.casefold() == pattern.casefold()
        elif match_type == 'merchant_exact':
            value = txn['merchant_clean']
            matched = bool(value) and value.casefold() == pattern.casefold()
        elif match_type == 'contains':
            matched = pattern.casefold() in raw_description.casefold()
        elif match_type == 'regex':
            try:
                matched = re.search(pattern, raw_description, re.I) is not None
            except re.error:
                matched = False
        else:
            matched = False

        if matched:
            return rule
    return None


def apply_rules(conn: sqlite3.Connection, txn_ids: list[int] | None = None) -> int:
    selected_ids = None if txn_ids is None else set(txn_ids)
    if selected_ids == set():
        return 0
    rows = conn.execute(
        "SELECT * FROM transactions WHERE classified_by='none' ORDER BY id"
    ).fetchall()
    classified = 0
    with _atomic(conn):
        for txn in rows:
            if selected_ids is not None and txn['id'] not in selected_ids:
                continue
            rule = find_rule(conn, txn)
            if rule is None:
                continue
            old = {'category_id': txn['category_id'], 'rule_id': txn['rule_id'],
                   'classified_by': txn['classified_by']}
            cursor = conn.execute(
                "UPDATE transactions SET category_id=?, rule_id=?, classified_by='rule' WHERE id=? AND classified_by='none'",
                (rule['category_id'], rule['id'], txn['id']),
            )
            if not cursor.rowcount:
                continue
            conn.execute('UPDATE merchant_rules SET hit_count=hit_count+1 WHERE id=?',
                         (rule['id'],))
            _audit(conn, 'transaction', txn['id'], 'classify_rule', old,
                   {'category_id': rule['category_id'], 'rule_id': rule['id'],
                    'classified_by': 'rule'})
            classified += 1
    return classified

def classify_remaining(conn: sqlite3.Connection,
                       txn_ids: list[int] | None = None) -> int:
    selected_ids = None if txn_ids is None else list(dict.fromkeys(txn_ids))
    if selected_ids == []:
        return 0
    category_ids = {
        row['name']: row['id']
        for row in conn.execute(
            "SELECT id, name FROM categories WHERE is_active=1 AND name IN ('Other income', 'Other expense')"
        )
    }
    if not category_ids:
        return 0
    sql = "SELECT id, direction, category_id, classified_by, rule_id FROM transactions WHERE classified_by='none' AND category_id IS NULL"
    params: list[int] = []
    if selected_ids is not None:
        placeholders = ','.join('?' for _ in selected_ids)
        sql += f' AND id IN ({placeholders})'
        params.extend(selected_ids)
    rows = conn.execute(sql, params).fetchall()
    classified = 0
    with _atomic(conn):
        for row in rows:
            name = 'Other income' if row['direction'] == 'in' else 'Other expense'
            category_id = category_ids.get(name)
            if category_id is None:
                continue
            cursor = conn.execute(
                "UPDATE transactions SET category_id=?, classified_by='system', rule_id=NULL WHERE id=? AND classified_by='none' AND category_id IS NULL",
                (category_id, row['id']),
            )
            if cursor.rowcount:
                _audit(conn, 'transaction', row['id'], 'classify_system',
                       {'category_id': row['category_id'], 'rule_id': row['rule_id'],
                        'classified_by': row['classified_by']},
                       {'category_id': category_id, 'rule_id': None,
                        'classified_by': 'system'})
                classified += cursor.rowcount
    return classified

def _learn_rules_from_manual(conn: sqlite3.Connection) -> int:
    """Learn safe merchant and description rules from manual choices."""
    groups: dict[tuple[str, str], tuple[str, set[int]]] = {}
    merchant_groups: dict[tuple[str, str], tuple[str, set[int]]] = {}
    rows = conn.execute(
        '''SELECT t.raw_description, t.merchant_clean, t.direction, t.category_id
           FROM transactions AS t
           JOIN categories AS c ON c.id=t.category_id
           WHERE t.classified_by='manual'
             AND c.is_active=1
             AND t.raw_description IS NOT NULL
             AND trim(t.raw_description) != ''
           ORDER BY t.id'''
    )
    for row in rows:
        pattern = row['raw_description'].strip()
        identity = row['direction'], pattern.casefold()
        group = groups.setdefault(identity, (pattern, set()))
        group[1].add(row['category_id'])
        merchant = (row['merchant_clean'] or '').strip()
        if merchant:
            merchant_identity = row['direction'], merchant.casefold()
            merchant_group = merchant_groups.setdefault(
                merchant_identity,
                (merchant, set()),
            )
            merchant_group[1].add(row['category_id'])

    learned = 0
    for (direction, _), (merchant, categories) in merchant_groups.items():
        if len(categories) != 1:
            continue
        category_id = next(iter(categories))
        existing = conn.execute(
            '''SELECT category_id FROM merchant_rules
               WHERE match_type='merchant_exact'
                 AND lower(pattern)=lower(?)
                 AND direction_filter IN (?, 'any')
                 AND is_active=1''',
            (merchant, direction),
        ).fetchall()
        if existing:
            continue
        add_rule(
            conn,
            'merchant_exact',
            merchant,
            category_id,
            direction,
            priority=5,
        )
        learned += 1

    for (direction, _), (pattern, categories) in groups.items():
        if len(categories) != 1:
            continue
        category_id = next(iter(categories))
        same_pattern_rules = conn.execute(
            '''SELECT pattern FROM merchant_rules
               WHERE match_type='contains'
                 AND direction_filter IN (?, 'any')
                 AND is_active=1''',
            (direction,),
        ).fetchall()
        if any(
            isinstance(rule['pattern'], str)
            and rule['pattern'].casefold() == pattern.casefold()
            for rule in same_pattern_rules
        ):
            continue

        add_rule(
            conn,
            'contains',
            pattern,
            category_id,
            direction,
            priority=10,
        )
        learned += 1
    return learned



def learn_rules_from_manual(conn: sqlite3.Connection) -> int:
    with _atomic(conn):
        return _learn_rules_from_manual(conn)


def get_review_groups(conn: sqlite3.Connection) -> list[dict]:
    """Group unclassified transactions by merchant or counterparty."""
    groups: dict[tuple[str, str, str, str], dict] = {}
    rows = conn.execute(
        '''SELECT t.*, COALESCE(NULLIF(a.currency,''),'MNT') AS currency
           FROM transactions AS t
           LEFT JOIN accounts AS a ON a.id=t.account_id
           LEFT JOIN categories AS c ON c.id=t.category_id
           WHERE t.category_id IS NULL AND t.classified_by='none'
             AND (c.type IS NULL OR c.type!='fee')
           ORDER BY t.id'''
    )
    for row in rows:
        merchant_value = row['merchant_clean'] or ''
        merchant = merchant_value.strip()
        useful_merchant = (
            bool(merchant)
            and len(merchant) > 1
            and merchant.casefold() != 'худалдан авалт'
        )
        if useful_merchant:
            key_type, key = 'merchant', merchant
        else:
            key_type = 'counterparty'
            key = row['counterparty_account'] or 'unknown'

        direction = row['direction']
        currency = str(row['currency'] or 'MNT').upper()
        identity = key_type, key, direction, currency
        group = groups.setdefault(
            identity,
            {
                'key_type': key_type,
                'key': key,
                'direction': direction,
                'currency': currency,
                'count': 0,
                'total': 0,
                'sample_desc': row['raw_description'],
                'txn_ids': [],
            },
        )
        group['count'] += 1
        group['total'] += abs(row['amount'])
        group['txn_ids'].append(row['id'])

    return sorted(groups.values(), key=lambda group: -group['total'])


def _remember_scope(rows: list[Any], remember: str) -> tuple[str, str, str]:
    if remember not in ('merchant', 'counterparty'):
        raise ValueError('Invalid remember option')
    field = 'merchant_clean' if remember == 'merchant' else 'counterparty_account'
    values = {(row[field] or '').strip() for row in rows}
    values.discard('')
    directions = {row['direction'] for row in rows}
    if len(values) != 1 or any(not (row[field] or '').strip() for row in rows):
        raise ValueError('Remembered rule requires one non-empty merchant/counterparty across selection')
    if len(directions) != 1:
        raise ValueError('Remembered rule requires one transaction direction')
    return ('merchant_exact' if remember == 'merchant' else 'counterparty_account',
            next(iter(values)), next(iter(directions)) )


def _check_rule_conflict(conn: Any, match_type: str, pattern: str,
                         direction: str, category_id: int) -> bool:
    rules = conn.execute(
        '''SELECT id, pattern, category_id, direction_filter FROM merchant_rules
           WHERE match_type=? AND is_active=1
             AND direction_filter IN (?, 'any')''',
        (match_type, direction),
    ).fetchall()
    for rule in rules:
        if (rule['pattern'] or '').casefold() != pattern.casefold():
            continue
        if rule['category_id'] != category_id:
            raise ValueError('An active rule already assigns this value to another category')
        return True
    return False



def preview_rule(conn: Any, match_type: str, pattern: str, category_id: int,
                 direction_filter: str = 'any') -> dict:
    if match_type not in {'counterparty_account', 'merchant_exact', 'contains', 'regex'}:
        raise ValueError('Invalid match type')
    if direction_filter not in {'in', 'out', 'any'}:
        raise ValueError('Invalid direction filter')
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError('Pattern must be non-empty text')
    pattern = pattern.strip()
    _rule_category_valid(conn, category_id, direction_filter)
    if match_type == 'regex':
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f'Invalid regex: {exc}') from exc
    rule_shape = {'match_type': match_type, 'pattern': pattern}
    candidates = conn.execute("SELECT * FROM transactions WHERE classified_by='none' ORDER BY id").fetchall()
    matched = [row for row in candidates
               if direction_filter in ('any', row['direction']) and _matches_rule(rule_shape, row)]
    return {'count': len(matched), 'total': sum(abs(row['amount']) for row in matched),
            'sample_desc': (matched[0]['raw_description'] or '')[:160] if matched else '',
            'direction': direction_filter}


def preview_rule_scope(conn: Any, txn_ids: list[int], remember: str,
                       category_id: int) -> dict:
    requested_ids = list(dict.fromkeys(txn_ids))
    if not requested_ids:
        raise ValueError('Select at least one transaction')
    rows_by_id = {}
    for offset in range(0, len(requested_ids), 900):
        batch = requested_ids[offset:offset + 900]
        placeholders = ','.join('?' for _ in batch)
        rows_by_id.update({row['id']: row for row in conn.execute(
            f'SELECT * FROM transactions WHERE id IN ({placeholders}) ORDER BY id',
            batch,
        ).fetchall()})
    rows = [rows_by_id[txn_id] for txn_id in requested_ids if txn_id in rows_by_id]
    if len(rows) != len(requested_ids):
        raise ValueError('One or more transactions no longer exist')
    match_type, pattern, direction = _remember_scope(rows, remember)
    _category(conn, category_id, direction)
    duplicate = _check_rule_conflict(conn, match_type, pattern, direction, category_id)
    rule_shape = {'match_type': match_type, 'pattern': pattern,
                  'direction_filter': direction}
    candidates = conn.execute(
        "SELECT * FROM transactions WHERE classified_by='none' ORDER BY id"
    ).fetchall()
    matched = [row for row in candidates if row['direction'] == direction and
               _matches_rule(rule_shape, row)]
    return {'match_type': match_type, 'pattern': pattern, 'direction': direction,
            'count': len(matched), 'total': sum(abs(row['amount']) for row in matched),
            'sample_desc': (matched[0]['raw_description'] or '')[:160] if matched else '',
            'already_exists': duplicate}


def _matches_rule(rule: Any, txn: Any) -> bool:
    pattern = rule['pattern']
    kind = rule['match_type']
    if kind == 'merchant_exact':
        value = txn['merchant_clean'] or ''
        return bool(value) and value.casefold() == pattern.casefold()
    if kind == 'counterparty_account':
        value = txn['counterparty_account'] or ''
        return bool(value) and value.casefold() == pattern.casefold()
    if kind == 'contains':
        return pattern.casefold() in (txn['raw_description'] or '').casefold()
    if kind == 'regex':
        try:
            return re.search(pattern, txn['raw_description'] or '', re.I) is not None
        except re.error:
            return False
    return False


def set_category(conn: sqlite3.Connection, txn_ids: list[int],
                 category_id: int | None,
                 remember: str | None = None) -> int:
    if remember not in (None, 'merchant', 'counterparty'):
        raise ValueError('Invalid remember option')
    requested_ids = list(dict.fromkeys(txn_ids))
    if not requested_ids:
        return 0
    with _atomic(conn):
        rows_by_id = {}
        for offset in range(0, len(requested_ids), 900):
            batch = requested_ids[offset:offset + 900]
            placeholders = ','.join('?' for _ in batch)
            rows_by_id.update({row['id']: row for row in conn.execute(
                f'SELECT * FROM transactions WHERE id IN ({placeholders})', batch
            ).fetchall()})
        if len(rows_by_id) != len(requested_ids):
            raise ValueError('One or more transactions no longer exist')
        rows = [rows_by_id[txn_id] for txn_id in requested_ids]
        if category_id is not None:
            for row in rows:
                _category(conn, category_id, row['direction'])
        rule_shape = None
        if remember is not None and category_id is not None:
            match_type, pattern, direction = _remember_scope(rows, remember)
            _category(conn, category_id, direction)
            already_exists = _check_rule_conflict(conn, match_type, pattern, direction, category_id)
            rule_shape = (match_type, pattern, direction, already_exists)
        for row in rows:
            old = {'category_id': row['category_id'], 'classified_by': row['classified_by'],
                   'rule_id': row['rule_id']}
            next_source = 'none' if category_id is None else 'manual'
            if old != {'category_id': category_id, 'classified_by': next_source, 'rule_id': None}:
                conn.execute(
                    'UPDATE transactions SET category_id=?, classified_by=?, rule_id=NULL WHERE id=?',
                    (category_id, next_source, row['id']),
                )
                _audit(conn, 'transaction', row['id'], 'manual_classify', old,
                       {'category_id': category_id, 'classified_by': next_source, 'rule_id': None})
        if rule_shape is not None:
            match_type, pattern, direction, already_exists = rule_shape
            if not already_exists:
                add_rule(conn, match_type, pattern, category_id, direction)
        selected = set(requested_ids)
        other_ids = [row['id'] for row in conn.execute(
            "SELECT id FROM transactions WHERE classified_by='none' ORDER BY id"
        ) if row['id'] not in selected]
        apply_rules(conn, other_ids)
    return len(rows)
