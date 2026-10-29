"""Consent-gated AI suggestion workflow; network behavior is always mocked."""

import json
import logging
from urllib.error import HTTPError

import pytest

from core import ai_classify
from core.ai_classify import (
    AIClassificationError,
    approve_ai_suggestions,
    mask_sensitive_text,
    prepare_expense_preview,
)
from core.db import get_conn, init_db


def _add_expense(conn, description, merchant, category_id, classified_by='system', account_id=None):
    return int(conn.execute(
        '''INSERT INTO transactions
           (account_id, txn_datetime, amount, direction, balance_after, raw_description,
            counterparty_account, merchant_clean, category_id, classified_by)
           VALUES (?, '2026-10-01 10:00:00', -1250, 'out', 10000, ?,
                   'PRIVATE-ACCOUNT', ?, ?, ?) RETURNING id''',
        (account_id, description, merchant, category_id, classified_by),
    ).fetchone()[0])


def _ready_connection():
    conn = get_conn(':memory:')
    init_db(conn)
    other_id = conn.execute("SELECT id FROM categories WHERE name='Other expense'").fetchone()[0]
    groceries_id = conn.execute("SELECT id FROM categories WHERE name='Groceries / Food'").fetchone()[0]
    ids = {
        'shop': _add_expense(conn, 'Qpay SHOP purchase', 'Shop', other_id),
        'unknown': _add_expense(conn, 'Unknown service', 'Unknown', other_id),
        'manual': _add_expense(conn, 'My manual choice', 'Manual', groceries_id, 'manual'),
    }
    conn.commit()
    return conn, ids, other_id, groceries_id


def _predictions(groups, category_id):
    return {
        str(group['id']): {
            'category_id': category_id,
            'confidence': 0.95 if group['merchant'] == 'Shop' else 0.60,
            'reason': 'Туршилтын ангилал',
        }
        for group in groups
    }


def test_preview_analyze_review_approve_and_retry_are_idempotent(monkeypatch):
    conn, ids, other_id, groceries_id = _ready_connection()
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    preview = prepare_expense_preview(conn, '2026-10-01', '2026-10-31')
    calls = []

    def fake_request(groups, categories, api_key, model):
        calls.append((groups, api_key, model))
        assert api_key == 'test-key'
        return _predictions(groups, groceries_id)

    monkeypatch.setattr(ai_classify, '_request_batch', fake_request)
    result = ai_classify.analyze_expenses(
        conn, '2026-10-01', '2026-10-31', consent=True,
        preview_fingerprint=preview['fingerprint'],
    )
    assert result == {
        'run_id': 1, 'transactions': 2, 'mappings': 2,
        'auto_classified': 0, 'needs_review': 2,
    }
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM ai_category_mappings').fetchone()[0] == 0
    for txn_id in (ids['shop'], ids['unknown']):
        row = conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=?', (txn_id,)).fetchone()
        assert (row['category_id'], row['classified_by']) == (other_id, 'system')

    shop_mapping = conn.execute(
        "SELECT id FROM ai_expense_mappings WHERE run_id=? AND match_value='Shop'",
        (result['run_id'],),
    ).fetchone()[0]
    approved = approve_ai_suggestions(conn, result['run_id'], [
        {'mapping_id': int(shop_mapping), 'category_id': groceries_id},
    ])
    assert approved == {'approved': 1, 'auto_classified': 1, 'needs_review': 1}
    shop = conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=?', (ids['shop'],)).fetchone()
    unknown = conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=?', (ids['unknown'],)).fetchone()
    manual = conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=?', (ids['manual'],)).fetchone()
    assert (shop['category_id'], shop['classified_by']) == (groceries_id, 'rule')
    assert (unknown['category_id'], unknown['classified_by']) == (other_id, 'system')
    assert (manual['category_id'], manual['classified_by']) == (groceries_id, 'manual')
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 1

    repeated = ai_classify.analyze_expenses(
        conn, '2026-10-01', '2026-10-31', consent=True,
        preview_fingerprint=preview['fingerprint'],
    )
    assert repeated['run_id'] == result['run_id']
    assert len(calls) == 1
    approve_ai_suggestions(conn, result['run_id'], [
        {'mapping_id': int(shop_mapping), 'category_id': groceries_id},
    ])
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM transactions WHERE classified_by="manual"').fetchone()[0] == 1
    conn.close()


def test_no_consent_and_stale_preview_make_zero_network_calls(monkeypatch):
    conn, _, _, _ = _ready_connection()
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    calls = []
    monkeypatch.setattr(ai_classify, '_request_batch', lambda *args: calls.append(args))
    with pytest.raises(AIClassificationError, match='зөвшөөрөл'):
        ai_classify.analyze_expenses(conn, '2026-10-01', '2026-10-31')
    preview = prepare_expense_preview(conn, '2026-10-01', '2026-10-31')
    with conn:
        conn.execute("UPDATE transactions SET raw_description='changed' WHERE merchant_clean='Shop'")
    with pytest.raises(AIClassificationError, match='өөрчлөгдсөн'):
        ai_classify.analyze_expenses(
            conn, '2026-10-01', '2026-10-31', consent=True,
            preview_fingerprint=preview['fingerprint'],
        )
    assert calls == []
    assert conn.execute('SELECT COUNT(*) FROM ai_analysis_runs').fetchone()[0] == 0
    conn.close()


def test_sensitive_patterns_are_masked_and_payload_has_no_workbook_or_balance(monkeypatch):
    raw = 'IBAN GB82 WEST 1234 5698 7654 32 acct 123456789012 phone +976 99112233 or 99112233 email jane@example.com'
    masked = mask_sensitive_text(raw)
    assert '[IBAN]' in masked
    assert '[ДАНС]' in masked
    assert masked.count('[УТАС]') >= 2
    assert '[И-МЭЙЛ]' in masked
    assert '123456789012' not in masked and 'jane@example.com' not in masked

    conn, _, other_id, _ = _ready_connection()
    _add_expense(conn, raw, 'Private 987654321098', other_id)
    conn.commit()
    preview = prepare_expense_preview(conn, '2026-10-01', '2026-10-31')
    serialized = json.dumps(preview['payload'], ensure_ascii=False)
    assert 'GB82' not in serialized and 'jane@example.com' not in serialized
    assert 'PRIVATE-ACCOUNT' not in serialized
    assert 'balance_after' not in serialized
    assert 'workbook' not in serialized.lower()
    assert preview['payload']['expenses'][0]['currency'] == 'MNT'
    conn.close()


def test_openai_request_defensively_masks_direct_batch(monkeypatch):
    sent = {}

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self):
            response = {'choices': [{'message': {'content': json.dumps({
                'classifications': [{'id': 'g0', 'category_id': 4,
                                     'confidence': 0.9, 'reason': 'Хүнс'}],
            })}}]}
            return json.dumps(response).encode()

    def fake_urlopen(request, timeout):
        sent['body'] = request.data.decode()
        sent['timeout'] = timeout
        assert request.get_header('Authorization') == 'Bearer test-key'
        return FakeResponse()

    monkeypatch.setattr(ai_classify, 'urlopen', fake_urlopen)
    result = ai_classify._request_batch(
        [{'id': 'g0', 'merchant': 'Vendor 123456789012',
          'sample_descriptions': ['Call +976 99112233 jane@example.com'],
          'first_date': '2026-10-01', 'last_date': '2026-10-01',
          'transaction_count': 1, 'total_amount': '12.50', 'currency': 'MNT'}],
        [{'id': 4, 'name': 'Groceries'}], 'test-key', ai_classify.DEFAULT_MODEL,
    )
    assert '123456789012' not in sent['body']
    assert '+976' not in sent['body'] and 'jane@example.com' not in sent['body']
    assert 'balance_after' not in sent['body']
    assert result['g0']['category_id'] == 4
    assert sent['timeout'] == 60


@pytest.mark.parametrize('classifications', [
    [{'id': 'unknown', 'category_id': 4, 'confidence': 0.9, 'reason': 'x'}],
    [
        {'id': 'g0', 'category_id': 4, 'confidence': 0.9, 'reason': 'x'},
        {'id': 'g0', 'category_id': 4, 'confidence': 0.8, 'reason': 'y'},
    ],
    [{'id': 'g0', 'category_id': 999, 'confidence': 0.9, 'reason': 'x'}],
    [{'id': 'g0', 'category_id': 4, 'confidence': float('nan'), 'reason': 'x'}],
    [{'id': 'g0', 'category_id': 4, 'confidence': True, 'reason': 'x'}],
])
def test_invalid_or_duplicate_model_response_is_rejected(monkeypatch, classifications):
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self):
            return json.dumps({'choices': [{'message': {'content': json.dumps({'classifications': classifications})}}]}).encode()
    monkeypatch.setattr(ai_classify, 'urlopen', lambda *args, **kwargs: FakeResponse())
    with pytest.raises(AIClassificationError):
        ai_classify._request_batch(
            [{'id': 'g0', 'merchant': 'Shop', 'sample_descriptions': []}],
            [{'id': 4, 'name': 'Food'}], 'test-key', ai_classify.DEFAULT_MODEL,
        )


def test_invalid_json_response_is_rejected(monkeypatch):
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self): return b'{bad-json'
    monkeypatch.setattr(ai_classify, 'urlopen', lambda *args, **kwargs: FakeResponse())
    with pytest.raises(AIClassificationError, match='унших боломжгүй'):
        ai_classify._request_batch([{'id': 'g0'}], [{'id': 4, 'name': 'Food'}], 'key', ai_classify.DEFAULT_MODEL)


@pytest.mark.parametrize('failure', ['timeout', '429'])
def test_network_failures_are_safe_and_leave_database_unchanged(monkeypatch, caplog, failure):
    conn, ids, other_id, _ = _ready_connection()
    monkeypatch.setenv('OPENAI_API_KEY', 'sensitive-api-key')
    preview = prepare_expense_preview(conn, '2026-10-01', '2026-10-31')
    secret_body = 'sensitive-api-key private-account'
    def fail(request, timeout):
        if failure == 'timeout':
            raise TimeoutError(secret_body)
        raise HTTPError(request.full_url, 429, secret_body, {}, None)
    monkeypatch.setattr(ai_classify, 'urlopen', fail)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AIClassificationError):
            ai_classify.analyze_expenses(
                conn, '2026-10-01', '2026-10-31', consent=True,
                preview_fingerprint=preview['fingerprint'],
            )
    assert secret_body not in caplog.text
    assert conn.execute('SELECT COUNT(*) FROM ai_analysis_runs').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 0
    for txn_id in (ids['shop'], ids['unknown']):
        row = conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=?', (txn_id,)).fetchone()
        assert (row['category_id'], row['classified_by']) == (other_id, 'system')
    conn.close()


def test_partial_batch_failure_writes_nothing_and_keeps_manual_classification(monkeypatch):
    conn, ids, other_id, groceries_id = _ready_connection()
    for i in range(3):
        _add_expense(conn, f'Purchase {i}', f'Merchant {i}', other_id)
    conn.commit()
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    monkeypatch.setattr(ai_classify, 'MAX_GROUPS_PER_REQUEST', 1)
    preview = prepare_expense_preview(conn, '2026-10-01', '2026-10-31')
    calls = 0
    def partial(groups, categories, api_key, model):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AIClassificationError('safe batch failure')
        return _predictions(groups, groceries_id)
    monkeypatch.setattr(ai_classify, '_request_batch', partial)
    with pytest.raises(AIClassificationError):
        ai_classify.analyze_expenses(
            conn, '2026-10-01', '2026-10-31', consent=True,
            preview_fingerprint=preview['fingerprint'],
        )
    assert calls == 2
    assert conn.execute('SELECT COUNT(*) FROM ai_analysis_runs').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM ai_expense_mappings').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM ai_category_mappings').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 0
    manual = conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=?', (ids['manual'],)).fetchone()
    assert (manual['category_id'], manual['classified_by']) == (groceries_id, 'manual')
    conn.close()


def test_approve_batch_validates_all_ids_and_categories_before_writing(monkeypatch):
    conn, _, _, groceries_id = _ready_connection()
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    preview = prepare_expense_preview(conn, '2026-10-01', '2026-10-31')
    monkeypatch.setattr(ai_classify, '_request_batch', lambda groups, categories, *_: _predictions(groups, groceries_id))
    run = ai_classify.analyze_expenses(conn, '2026-10-01', '2026-10-31', consent=True, preview_fingerprint=preview['fingerprint'])
    rows = conn.execute('SELECT id FROM ai_expense_mappings WHERE run_id=? ORDER BY id', (run['run_id'],)).fetchall()
    first_id, second_id = [int(row['id']) for row in rows]
    cases = [
        [{'mapping_id': first_id, 'category_id': groceries_id}, {'mapping_id': first_id, 'category_id': groceries_id}],
        [{'mapping_id': first_id, 'category_id': groceries_id}, {'mapping_id': 999999, 'category_id': groceries_id}],
        [{'mapping_id': first_id, 'category_id': groceries_id}, {'mapping_id': second_id, 'category_id': 999999}],
    ]
    for decisions in cases:
        with pytest.raises(AIClassificationError):
            approve_ai_suggestions(conn, run['run_id'], decisions)
        assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM ai_category_mappings').fetchone()[0] == 0
    conn.close()


def test_auto_apply_requires_explicit_opt_in_and_threshold(monkeypatch):
    conn, ids, other_id, groceries_id = _ready_connection()
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    preview = prepare_expense_preview(conn, '2026-10-01', '2026-10-31')
    monkeypatch.setattr(ai_classify, '_request_batch', lambda groups, categories, *_: _predictions(groups, groceries_id))
    result = ai_classify.analyze_expenses(
        conn, '2026-10-01', '2026-10-31', consent=True,
        preview_fingerprint=preview['fingerprint'], auto_apply=True,
    )
    assert result['auto_classified'] == 1
    assert result['needs_review'] == 1
    assert conn.execute('SELECT COUNT(*) FROM merchant_rules').fetchone()[0] == 1
    applied = conn.execute('SELECT classified_by FROM transactions WHERE id=?', (ids['shop'],)).fetchone()[0]
    untouched = conn.execute('SELECT category_id, classified_by FROM transactions WHERE id=?', (ids['unknown'],)).fetchone()
    assert applied == 'rule'
    assert (untouched['category_id'], untouched['classified_by']) == (other_id, 'system')
    conn.close()


def test_missing_group_prediction_is_rejected(monkeypatch):
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self):
            return json.dumps({'choices': [{'message': {'content': '{\"classifications\":[]}'}}]}).encode()
    monkeypatch.setattr(ai_classify, 'urlopen', lambda *args, **kwargs: FakeResponse())
    with pytest.raises(AIClassificationError, match='бүх зарлагад'):
        ai_classify._request_batch(
            [{'id': 'g0', 'merchant': 'Shop'}],
            [{'id': 4, 'name': 'Food'}], 'key', ai_classify.DEFAULT_MODEL,
        )
