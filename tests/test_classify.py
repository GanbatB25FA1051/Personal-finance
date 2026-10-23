"""Tests for rule matching, remembered choices, and review groups."""

import pytest

from core.classify import (
    add_rule,
    apply_rules,
    find_rule,
    get_review_groups,
    classify_remaining,
    learn_rules_from_manual,
    set_category,
)
from core.db import get_conn, init_db


@pytest.fixture
def conn():
    db = get_conn(':memory:')
    init_db(db)
    db.executemany(
        '''INSERT INTO transactions
           (merchant_clean, raw_description, counterparty_account, amount, direction)
           VALUES (?, ?, ?, ?, ?)''',
        [
            ('shop', 'Shop food', None, -500, 'out'),
            ('shop', 'Shop food', None, -300, 'out'),
            ('a', 'A', '112700229720', -1000, 'out'),
            ('shop', 'Refund shop', None, 200, 'in'),
        ],
    )
    db.commit()
    yield db
    db.close()


def test_priority_and_direction_filter(conn) -> None:
    add_rule(conn, 'contains', 'shop', 1, 'in', 1)
    chosen = add_rule(conn, 'merchant_exact', 'SHOP', 6, 'out', 10)
    add_rule(conn, 'contains', 'food', 7, 'out', 20)

    assert find_rule(
        conn, conn.execute('SELECT * FROM transactions WHERE id=1').fetchone()
    )['id'] == chosen
    assert find_rule(
        conn, conn.execute('SELECT * FROM transactions WHERE id=4').fetchone()
    )['category_id'] == 1
    assert apply_rules(conn) == 3
    assert conn.execute(
        'SELECT category_id FROM transactions WHERE id=4'
    ).fetchone()[0] == 1


def test_manual_rows_are_not_overwritten_and_merchant_rule_tags_others(conn) -> None:
    assert set_category(conn, [1], 6, 'merchant') == 1
    first = conn.execute(
        'SELECT category_id, classified_by FROM transactions WHERE id=1'
    ).fetchone()
    assert (first['category_id'], first['classified_by']) == (6, 'manual')

    add_rule(conn, 'contains', 'shop', 7, priority=1)
    assert apply_rules(conn) == 1
    row = conn.execute(
        'SELECT category_id, classified_by FROM transactions WHERE id=1'
    ).fetchone()
    assert (row['category_id'], row['classified_by']) == (6, 'manual')
    assert conn.execute(
        'SELECT category_id FROM transactions WHERE id=2'
    ).fetchone()[0] == 6


def test_counterparty_rule_handles_junk_merchant_and_tags_others(conn) -> None:
    conn.execute(
        '''INSERT INTO transactions
           (merchant_clean, raw_description, counterparty_account, amount, direction)
           VALUES (?, ?, ?, ?, ?)''',
        ('', 'A', '112700229720', -250, 'out'),
    )
    conn.commit()

    groups = get_review_groups(conn)
    junk_group = next(group for group in groups if group['key_type'] == 'counterparty')
    assert junk_group['key'] == '112700229720'

    assert set_category(conn, [3], 8, 'counterparty') == 1
    manual = conn.execute(
        'SELECT category_id, classified_by FROM transactions WHERE id=3'
    ).fetchone()
    automatic = conn.execute(
        'SELECT category_id, classified_by FROM transactions WHERE id=5'
    ).fetchone()
    assert (manual['category_id'], manual['classified_by']) == (8, 'manual')
    assert (automatic['category_id'], automatic['classified_by']) == (8, 'rule')
    assert conn.execute(
        "SELECT pattern FROM merchant_rules WHERE match_type='counterparty_account'"
    ).fetchone()[0] == '112700229720'


def test_invalid_regex_and_rule_inputs_are_rejected(conn) -> None:
    with pytest.raises(ValueError):
        add_rule(conn, 'regex', '[', 6)
    with pytest.raises(ValueError):
        add_rule(conn, 'unknown', 'shop', 6)
    with pytest.raises(ValueError):
        add_rule(conn, 'contains', 'shop', 999)


def test_review_groups_split_direction_and_sort_by_total(conn) -> None:
    groups = get_review_groups(conn)

    assert [(group['key_type'], group['key'], group['direction']) for group in groups] == [
        ('counterparty', '112700229720', 'out'),
        ('merchant', 'shop', 'out'),
        ('merchant', 'shop', 'in'),
    ]
    assert groups[0]['total'] == 1000
    assert groups[1]['txn_ids'] == [1, 2]
    assert groups[1]['count'] == 2
    assert groups[1]['total'] == 800
    assert groups[2]['total'] == 200


def test_invalid_merchant_names_fall_back_to_counterparty(conn) -> None:
    conn.execute(
        '''INSERT INTO transactions
           (merchant_clean, raw_description, counterparty_account, amount, direction)
           VALUES (?, ?, ?, ?, ?)''',
        ('Худалдан авалт', 'A', None, -50, 'out'),
    )
    conn.commit()

    groups = get_review_groups(conn)
    fallback = next(group for group in groups if group['key'] == 'unknown')
    assert fallback['key_type'] == 'counterparty'
    assert fallback['txn_ids'] == [5]


def test_empty_selection_and_clear_preserve_manual_intent(conn) -> None:
    add_rule(conn, 'contains', 'shop', 6)
    conn.execute(
        "UPDATE transactions SET classified_by='system', category_id=15 WHERE id=1"
    )
    conn.commit()

    assert apply_rules(conn, []) == 0
    assert apply_rules(conn) == 2
    assert conn.execute(
        'SELECT classified_by FROM transactions WHERE id=1'
    ).fetchone()[0] == 'system'

    set_category(conn, [2], None)
    row = conn.execute(
        'SELECT category_id, classified_by, rule_id FROM transactions WHERE id=2'
    ).fetchone()
    assert row['category_id'] is None
    assert row['classified_by'] == 'none'
    assert row['rule_id'] is None


def test_remember_deduplicates_active_rules(conn) -> None:
    set_category(conn, [1], 6, 'merchant')
    set_category(conn, [2], 6, 'merchant')

    assert conn.execute(
        'SELECT COUNT(*) FROM merchant_rules'
    ).fetchone()[0] == 1


def test_learn_rules_from_consistent_manual_descriptions(conn) -> None:
    set_category(conn, [1, 2], 6)

    assert learn_rules_from_manual(conn) == 2
    rules = {
        row['match_type']: tuple(row)
        for row in conn.execute(
            '''SELECT match_type, pattern, direction_filter, category_id
               FROM merchant_rules'''
        )
    }
    assert rules == {
        'merchant_exact': ('merchant_exact', 'shop', 'out', 6),
        'contains': ('contains', 'Shop food', 'out', 6),
    }

    conn.execute(
        '''INSERT INTO transactions
           (merchant_clean, raw_description, amount, direction)
           VALUES ('shop', 'Shop food - 2026', -250, 'out')'''
    )
    assert apply_rules(conn) == 1
    classified = conn.execute(
        'SELECT category_id, classified_by FROM transactions WHERE id=5'
    ).fetchone()
    assert tuple(classified) == (6, 'rule')

    assert learn_rules_from_manual(conn) == 0
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 2


def test_learning_skips_descriptions_manually_assigned_to_multiple_categories(conn) -> None:
    set_category(conn, [1], 6)
    set_category(conn, [2], 7)

    assert learn_rules_from_manual(conn) == 0
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 0


def test_fallback_classification_only_updates_unclassified_transactions(conn) -> None:
    conn.execute(
        "UPDATE transactions SET category_id=6, classified_by='manual' WHERE id=1"
    )
    conn.execute(
        "UPDATE transactions SET category_id=15, classified_by='system' WHERE id=3"
    )
    conn.commit()

    assert classify_remaining(conn) == 2
    rows = conn.execute(
        'SELECT id, category_id, classified_by FROM transactions ORDER BY id'
    ).fetchall()
    assert tuple(rows[0]) == (1, 6, 'manual')
    assert tuple(rows[1]) == (2, 14, 'system')
    assert tuple(rows[2]) == (3, 15, 'system')
    assert tuple(rows[3]) == (4, 5, 'system')
