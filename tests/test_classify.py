"""Tests for rule matching, remembered choices, and review groups."""

import pytest

from core.classify import (
    add_rule,
    apply_rules,
    find_rule,
    get_review_groups,
    classify_remaining,
    learn_rules_from_manual,
    preview_rule_scope,
    reapply_rules,
    set_category,
    set_category_active,
    update_rule,
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

    add_rule(conn, 'contains', 'shop', 7, 'out', priority=1)
    # The remembered merchant rule already classified the second matching row.
    assert conn.execute("SELECT classified_by FROM transactions WHERE id=2").fetchone()[0] == 'rule'
    assert apply_rules(conn) == 0
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
    add_rule(conn, 'contains', 'shop', 6, 'out')
    conn.execute(
        "UPDATE transactions SET classified_by='system', category_id=15 WHERE id=1"
    )
    conn.commit()

    assert apply_rules(conn, []) == 0
    assert apply_rules(conn) == 1
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



def test_mixed_bulk_remember_is_rejected_before_any_manual_update(conn) -> None:
    with pytest.raises(ValueError, match='one non-empty'):
        set_category(conn, [1, 3], 6, 'merchant')
    with pytest.raises(ValueError, match='one transaction direction'):
        set_category(conn, [1, 4], 16, 'merchant')
    assert [row[0] for row in conn.execute('SELECT category_id FROM transactions WHERE id IN (1,3,4) ORDER BY id').fetchall()] == [None, None, None]
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 0


def test_blank_merchant_cannot_be_remembered(conn) -> None:
    conn.execute("UPDATE transactions SET merchant_clean='' WHERE id=3")
    conn.commit()
    with pytest.raises(ValueError, match='non-empty'):
        set_category(conn, [3], 8, 'merchant')
    assert conn.execute('SELECT category_id FROM transactions WHERE id=3').fetchone()[0] is None
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 0


def test_remember_none_does_not_create_persistent_rule(conn) -> None:
    set_category(conn, [1], 6, remember=None)
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 0
    assert conn.execute('SELECT classified_by FROM transactions WHERE id=1').fetchone()[0] == 'manual'


def test_conflicting_rule_rejects_manual_save_atomically(conn) -> None:
    add_rule(conn, 'merchant_exact', 'shop', 7, 'out')
    with pytest.raises(ValueError, match='another category'):
        set_category(conn, [1], 6, 'merchant')
    assert conn.execute('SELECT category_id FROM transactions WHERE id=1').fetchone()[0] is None
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 1


def test_invalid_archived_and_direction_categories_are_rejected(conn) -> None:
    with pytest.raises(ValueError, match='direction'):
        set_category(conn, [1], 1)
    conn.execute('UPDATE categories SET is_active=0 WHERE id=6')
    conn.commit()
    with pytest.raises(ValueError, match='archived'):
        set_category(conn, [1], 6)
    with pytest.raises(ValueError, match='archived'):
        add_rule(conn, 'merchant_exact', 'shop', 6, 'out')


def test_manual_classification_survives_rule_reapply_and_audit_is_written(conn) -> None:
    set_category(conn, [1], 6)
    conn.execute("UPDATE transactions SET category_id=15, classified_by='system' WHERE id=3")
    conn.commit()
    add_rule(conn, 'contains', 'shop', 7, 'out', priority=1)
    assert apply_rules(conn) == 1
    before = conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=1').fetchone()
    assert tuple(before) == (6, 'manual')
    assert reapply_rules(conn) == 1
    assert tuple(conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=1').fetchone()) == (6, 'manual')
    assert tuple(conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=3').fetchone()) == (15, 'system')
    audit = conn.execute(
        "SELECT old_values, new_values, changed_at FROM classification_audit WHERE entity_type='transaction' AND entity_id=1 ORDER BY id LIMIT 1"
    ).fetchone()
    assert audit is not None and audit['old_values'] is not None and audit['new_values'] is not None
    assert audit['changed_at']


def test_remembered_rule_preview_reports_direction_count_total_and_sample(conn) -> None:
    preview = preview_rule_scope(conn, [1, 2], 'merchant', 6)
    assert preview['direction'] == 'out'
    assert preview['count'] == 2
    assert preview['total'] == 800
    assert preview['sample_desc'] == 'Shop food'
    assert not preview['already_exists']


def test_failed_audit_rolls_back_category_and_rule(monkeypatch, conn) -> None:
    import core.classify as classify

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError('audit unavailable')

    monkeypatch.setattr(classify, '_audit', fail_audit)
    with pytest.raises(RuntimeError, match='audit unavailable'):
        set_category(conn, [1], 6, 'merchant')
    assert tuple(conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=1').fetchone()) == (None, 'none')
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 0



def test_rule_update_and_archive_write_old_new_audit(conn) -> None:
    rule_id = add_rule(conn, 'merchant_exact', 'shop', 6, 'out')
    assert apply_rules(conn) == 2
    update_rule(conn, rule_id, category_id=7, priority=20)
    rule_audit = conn.execute(
        "SELECT old_values, new_values, changed_at FROM classification_audit WHERE entity_type='rule' AND entity_id=? ORDER BY id DESC LIMIT 1",
        (rule_id,),
    ).fetchone()
    assert rule_audit is not None and rule_audit['changed_at']
    assert '"category_id": 6' in rule_audit['old_values']
    assert '"category_id": 7' in rule_audit['new_values']
    assert conn.execute(
        "SELECT category_id FROM transactions WHERE id=2"
    ).fetchone()[0] == 7

    set_category_active(conn, 7, False)
    assert conn.execute('SELECT is_active FROM categories WHERE id=7').fetchone()[0] == 0
    assert conn.execute('SELECT is_active FROM merchant_rules WHERE id=?', (rule_id,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM classification_audit WHERE entity_type='category' AND entity_id=7 AND action='archive'"
    ).fetchone()[0] == 1


def test_failed_rule_audit_rolls_back_rule_update(monkeypatch, conn) -> None:
    import core.classify as classify

    rule_id = add_rule(conn, 'merchant_exact', 'shop', 6, 'out')
    conn.commit()
    def fail_audit(*_args, **_kwargs):
        raise RuntimeError('audit unavailable')
    monkeypatch.setattr(classify, '_audit', fail_audit)
    with pytest.raises(RuntimeError, match='audit unavailable'):
        update_rule(conn, rule_id, category_id=7)
    assert tuple(conn.execute(
        'SELECT category_id, priority, is_active FROM merchant_rules WHERE id=?',
        (rule_id,),
    ).fetchone()) == (6, 100, 1)



def test_review_group_totals_do_not_mix_currencies(conn) -> None:
    conn.execute(
        'INSERT INTO accounts(id, account_number, currency) VALUES (1, ?, ?)',
        ('998800000001', 'USD'),
    )
    conn.execute(
        '''INSERT INTO transactions(account_id, merchant_clean, raw_description, amount, direction)
           VALUES (1, 'shop', 'Shop USD', -2500, 'out')'''
    )
    conn.commit()
    groups = [
        group for group in get_review_groups(conn)
        if group['key_type'] == 'merchant' and group['key'].casefold() == 'shop'
        and group['direction'] == 'out'
    ]
    assert {(group['currency'], group['total']) for group in groups} == {
        ('MNT', 800), ('USD', 2500),
    }
