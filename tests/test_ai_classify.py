"""Test opt-in AI analysis without making external network requests."""

import json

from core import ai_classify
from core.db import get_conn, init_db


def _add_expense(
    conn,
    description: str,
    merchant: str,
    category_id: int,
    classified_by: str,
    counterparty: str,
) -> int:
    cursor = conn.execute(
        '''INSERT INTO transactions
           (txn_datetime, amount, direction, balance_after, raw_description,
            counterparty_account, merchant_clean, category_id, classified_by)
           VALUES ('2026-10-01 10:00:00', -1250, 'out', 10000, ?, ?, ?, ?, ?)''',
        (description, counterparty, merchant, category_id, classified_by),
    )
    return int(cursor.lastrowid)


def test_analyze_saves_confident_rules_and_leaves_uncertain_for_review(
    monkeypatch,
) -> None:
    conn = get_conn(':memory:')
    init_db(conn)
    other_id = conn.execute(
        "SELECT id FROM categories WHERE name='Other expense'"
    ).fetchone()[0]
    groceries_id = conn.execute(
        "SELECT id FROM categories WHERE name='Groceries / Food'"
    ).fetchone()[0]
    with conn:
        confident_id = _add_expense(
            conn, 'Qpay SHOP purchase', 'Shop', other_id, 'system', 'PRIVATE-ACCOUNT'
        )
        uncertain_id = _add_expense(
            conn, 'Unknown service', 'Unknown', other_id, 'system', 'PRIVATE-ACCOUNT'
        )
        manual_id = _add_expense(
            conn, 'My manual choice', 'Manual', groceries_id, 'manual', 'PRIVATE-ACCOUNT'
        )

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')

    def fake_request(groups, categories, api_key, model):
        assert api_key == 'test-key'
        assert model == ai_classify.DEFAULT_MODEL
        return {
            str(group['id']): {
                'category_id': groceries_id,
                'confidence': 0.95 if group['merchant'] == 'Shop' else 0.60,
                'reason': 'Туршилтын ангилал',
            }
            for group in groups
        }

    monkeypatch.setattr(ai_classify, '_request_batch', fake_request)
    result = ai_classify.analyze_expenses(conn, '2026-10-01', '2026-10-31')

    assert result == {
        'run_id': 1,
        'transactions': 2,
        'mappings': 2,
        'auto_classified': 1,
        'needs_review': 1,
    }
    confident = conn.execute(
        'SELECT category_id, classified_by FROM transactions WHERE id=?',
        (confident_id,),
    ).fetchone()
    assert (confident['category_id'], confident['classified_by']) == (
        groceries_id,
        'rule',
    )
    uncertain = conn.execute(
        'SELECT category_id, classified_by FROM transactions WHERE id=?',
        (uncertain_id,),
    ).fetchone()
    assert (uncertain['category_id'], uncertain['classified_by']) == (None, 'none')
    manual = conn.execute(
        'SELECT category_id, classified_by FROM transactions WHERE id=?',
        (manual_id,),
    ).fetchone()
    assert (manual['category_id'], manual['classified_by']) == (
        groceries_id,
        'manual',
    )
    assert conn.execute(
        'SELECT COUNT(*) FROM merchant_rules'
    ).fetchone()[0] == 1
    mappings = conn.execute(
        '''SELECT match_value, category_id, transaction_count, expense_total_minor
           FROM ai_expense_mappings
           WHERE run_id=? ORDER BY match_value''',
        (result['run_id'],),
    ).fetchall()
    assert len(mappings) == 2
    assert all(row['transaction_count'] == 1 for row in mappings)
    assert all(row['expense_total_minor'] == 1250 for row in mappings)
    analysis_run = conn.execute(
        '''SELECT period_start, period_end, transaction_count, mapping_count,
                  auto_classified, needs_review
           FROM ai_analysis_runs WHERE id=?''',
        (result['run_id'],),
    ).fetchone()
    assert tuple(analysis_run) == (
        '2026-10-01',
        '2026-10-31',
        2,
        2,
        1,
        1,
    )
    conn.close()


def test_openai_request_excludes_account_fields(monkeypatch) -> None:
    sent: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            content = {
                'classifications': [{
                    'id': 'g0',
                    'category_id': 4,
                    'confidence': 0.9,
                    'reason': 'Хүнсний худалдан авалт',
                }]
            }
            envelope = {'choices': [{'message': {'content': json.dumps(content)}}]}
            return json.dumps(envelope).encode('utf-8')

    def fake_urlopen(request, timeout):
        assert request.get_header('Authorization') == 'Bearer test-key'
        sent['body'] = request.data
        sent['timeout'] = timeout
        return FakeResponse()

    monkeypatch.setattr(ai_classify, 'urlopen', fake_urlopen)
    result = ai_classify._request_batch(
        [{
            'id': 'g0',
            'merchant': 'Grocery shop',
            'first_date': '2026-10-01',
            'last_date': '2026-10-01',
            'transaction_count': 1,
            'total_amount_mnt': '12.50',
            'sample_descriptions': ['Qpay grocery shop'],
        }],
        [{'id': 4, 'name': 'Groceries / Food'}],
        'test-key',
        ai_classify.DEFAULT_MODEL,
    )

    body = sent['body'].decode('utf-8')
    assert 'PRIVATE-ACCOUNT' not in body
    assert '"balance_after"' not in body
    assert result['g0']['category_id'] == 4
    assert sent['timeout'] == 60
