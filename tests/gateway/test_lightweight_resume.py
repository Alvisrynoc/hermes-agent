"""An attachment may omit transcript bodies without losing its native subscription or controls."""
import json

import pytest

from tests.gateway.test_surface_admission import _authority


@pytest.mark.asyncio
@pytest.mark.parametrize('omit', [True, False])
async def test_resume_honors_transcript_omission_and_keeps_the_real_message_count(tmp_path, monkeypatch, omit):
    from gateway.session_controls import AuthorityConnection

    authority = await _authority(tmp_path, monkeypatch, lambda event: None)
    body = ('large-transcript-marker ' * 50_000).rstrip()
    for _ in range(8):
        authority.db.append_message('s', 'user', body)
    connection = AuthorityConnection(authority, object(), {'user_id': 'viewer'})
    try:
        reply = await connection.dispatch({'id': 1, 'method': 'session.resume', 'params': {
            'session_id': 's', 'omit_messages': omit, 'defer_history': True, 'display_projection': True}})
        snapshot = reply['result']
        assert snapshot['message_count'] == 8
        assert snapshot['subscription_id'] in authority.sessions['s'].subscribers
        assert snapshot['pending'] == [] and snapshot['prompts'] == []
        assert snapshot['open_requests'] == []
        if omit:
            assert snapshot['messages'] == []
            assert 'display_messages' not in snapshot
            assert 'large-transcript-marker' not in json.dumps(snapshot)
            assert len(json.dumps(snapshot)) < 50_000
        else:
            assert len(snapshot['messages']) == 8
            assert len(snapshot['display_messages']) == 8
            assert snapshot['messages'][0]['content'] == body
    finally:
        await connection.close()
