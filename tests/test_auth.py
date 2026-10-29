"""Authentication session cleanup tests."""

from core.auth import clear_session_state


def test_logout_cleanup_removes_every_session_value():
    state = {
        'authenticated': True,
        'ai_external_consent': True,
        'pending_import_preview': {'file_data': b'synthetic'},
        'selected_transaction_ids': [1, 2],
    }
    clear_session_state(state)
    assert state == {}
