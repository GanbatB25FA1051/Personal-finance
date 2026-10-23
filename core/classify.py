"""Rule matching and manual transaction classification."""

import re
import sqlite3
from datetime import datetime


def add_rule(conn: sqlite3.Connection, match_type: str, pattern: str,
             category_id: int, direction_filter: str = 'any',
             priority: int = 100) -> int:
    """Validate and insert a classification rule."""
    if match_type not in {'counterparty_account', 'merchant_exact', 'contains', 'regex'}:
        raise ValueError('Invalid match type')
    if direction_filter not in {'in', 'out', 'any'}:
        raise ValueError('Invalid direction filter')
    if not isinstance(pattern, str):
        raise ValueError('Pattern must be text')
    if not conn.execute('SELECT 1 FROM categories WHERE id=?', (category_id,)).fetchone():
        raise ValueError('Category does not exist')
    if match_type == 'regex':
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f'Invalid regex: {exc}') from exc

    cursor = conn.execute(
        '''INSERT INTO merchant_rules
           (match_type, pattern, category_id, direction_filter, priority, created_at)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (match_type, pattern, category_id, direction_filter, priority,
         datetime.now().isoformat()),
    )
    return int(cursor.lastrowid)


def find_rule(conn: sqlite3.Connection, txn: sqlite3.Row) -> sqlite3.Row | None:
    """Return the first active rule matching a transaction."""
    raw_description = txn['raw_description'] or ''
    direction = txn['direction']

    for rule in conn.execute(
        'SELECT * FROM merchant_rules WHERE is_active=1 ORDER BY priority ASC, id ASC'
    ):
        if rule['direction_filter'] not in ('any', direction):
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
    """Apply matching rules to unclassified transactions only."""
    selected_ids = None if txn_ids is None else set(txn_ids)
    if selected_ids == set():
        return 0

    rows = conn.execute(
        "SELECT * FROM transactions WHERE classified_by='none' ORDER BY id"
    ).fetchall()
    classified = 0
    for txn in rows:
        if selected_ids is not None and txn['id'] not in selected_ids:
            continue
        rule = find_rule(conn, txn)
        if rule is None:
            continue

        conn.execute(
            "UPDATE transactions SET category_id=?, rule_id=?, classified_by='rule' WHERE id=?",
            (rule['category_id'], rule['id'], txn['id']),
        )
        conn.execute(
            'UPDATE merchant_rules SET hit_count=hit_count+1 WHERE id=?',
            (rule['id'],),
        )
        classified += 1
    return classified


def classify_remaining(conn: sqlite3.Connection,
                       txn_ids: list[int] | None = None) -> int:
    """Assign unmatched transactions to the default other income or expense."""
    selected_ids = None if txn_ids is None else list(dict.fromkeys(txn_ids))
    if selected_ids == []:
        return 0

    category_ids = {
        row['name']: row['id']
        for row in conn.execute(
            '''SELECT id, name FROM categories
               WHERE is_active=1 AND name IN ('Other income', 'Other expense')'''
        )
    }
    if not category_ids:
        return 0

    sql = """SELECT id, direction FROM transactions
             WHERE classified_by='none' AND category_id IS NULL"""
    params: list[int] = []
    if selected_ids is not None:
        placeholders = ','.join('?' for _ in selected_ids)
        sql += f' AND id IN ({placeholders})'
        params.extend(selected_ids)

    classified = 0
    for row in conn.execute(sql, params).fetchall():
        category_name = 'Other income' if row['direction'] == 'in' else 'Other expense'
        category_id = category_ids.get(category_name)
        if category_id is None:
            continue
        cursor = conn.execute(
            """UPDATE transactions
               SET category_id=?, classified_by='system', rule_id=NULL
               WHERE id=? AND classified_by='none' AND category_id IS NULL""",
            (category_id, row['id']),
        )
        classified += cursor.rowcount
    return classified


def learn_rules_from_manual(conn: sqlite3.Connection) -> int:
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
                 AND pattern=? COLLATE NOCASE
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


def get_review_groups(conn: sqlite3.Connection) -> list[dict]:
    """Group unclassified transactions by merchant or counterparty."""
    groups: dict[tuple[str, str, str], dict] = {}
    rows = conn.execute(
        '''SELECT t.* FROM transactions AS t
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
        identity = key_type, key, direction
        group = groups.setdefault(
            identity,
            {
                'key_type': key_type,
                'key': key,
                'direction': direction,
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


def set_category(conn: sqlite3.Connection, txn_ids: list[int],
                 category_id: int | None,
                 remember: str | None = None) -> int:
    """Manually classify transactions and optionally remember a matching rule."""
    if remember not in (None, 'merchant', 'counterparty'):
        raise ValueError('Invalid remember option')
    if category_id is not None and not conn.execute(
        'SELECT 1 FROM categories WHERE id=?', (category_id,)
    ).fetchone():
        raise ValueError('Category does not exist')

    requested_ids = list(dict.fromkeys(txn_ids))
    if not requested_ids:
        return 0
    placeholders = ','.join('?' for _ in requested_ids)
    rows_by_id = {
        row['id']: row
        for row in conn.execute(
            f'SELECT * FROM transactions WHERE id IN ({placeholders})',
            requested_ids,
        ).fetchall()
    }
    rows = [rows_by_id[txn_id] for txn_id in requested_ids if txn_id in rows_by_id]

    for row in rows:
        conn.execute(
            'UPDATE transactions SET category_id=?, classified_by=?, rule_id=NULL WHERE id=?',
            (category_id, 'none' if category_id is None else 'manual', row['id']),
        )

    if rows and category_id is not None and remember is not None:
        first_row = rows[0]
        if remember == 'merchant':
            match_type = 'merchant_exact'
            value = first_row['merchant_clean']
        else:
            match_type = 'counterparty_account'
            value = first_row['counterparty_account']

        if value:
            direction_filter = first_row['direction']
            duplicate = conn.execute(
                '''SELECT pattern FROM merchant_rules
                   WHERE match_type=? AND category_id=? AND direction_filter=?
                     AND is_active=1''',
                (match_type, category_id, direction_filter),
            ).fetchall()
            if not any(
                isinstance(rule['pattern'], str)
                and rule['pattern'].casefold() == value.casefold()
                for rule in duplicate
            ):
                add_rule(conn, match_type, value, category_id, direction_filter)

    selected = set(requested_ids)
    other_unclassified_ids = [
        row['id']
        for row in conn.execute(
            "SELECT id FROM transactions WHERE classified_by='none' ORDER BY id"
        )
        if row['id'] not in selected
    ]
    apply_rules(conn, other_unclassified_ids)
    return len(rows)
