"""Attached viewers recover native requests without changing ids or response ownership."""
import asyncio
from dataclasses import replace
import threading

import pytest

from tests.gateway.test_surface_admission import _authority
from tests.waiting import async_wait_until


class Peer:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(('method', 'params'), [
    ('sudo', {'command': 'sudo true'}),
    ('secret', {'env_var': 'EXAMPLE_KEY', 'prompt': 'Enter the key'}),
    ('clarify', {'questions': [{'qid': 'q0', 'question': 'Colour?'}, {'qid': 'q1', 'question': 'Size?'}]}),
])
async def test_resume_and_replay_keep_native_request_identity_and_locked_answers(tmp_path, monkeypatch, method, params):
    from gateway import session_server_requests as requests
    from gateway.session_controls import AuthorityConnection

    authority = await _authority(tmp_path, monkeypatch, lambda event: None)
    peer = Peer()
    owner = AuthorityConnection(authority, peer, {'user_id': 'owner'})
    outsider = AuthorityConnection(authority, Peer(), {'user_id': 'outsider'})
    await owner.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 's'}})
    events = authority.sessions['s'].event_stream
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(
        requests.ask_viewer_result, events, 's', method, params, cancelled=cancelled.is_set,
        qids=['q0', 'q1'] if method == 'clarify' else None))
    try:
        await async_wait_until(lambda: any(frame.get('method') == method for frame in peer.frames))
        frame = next(frame for frame in peer.frames if frame.get('method') == method)
        if method == 'clarify':
            assert requests.lock_answer(frame['id'], 'q0', 'blue',
                session_ok=lambda sid, stream: requests.may_settle(authority, owner.actor, sid, stream)) == ['q1']
        activity = await owner.dispatch({'id': 4, 'method': 'session.active_list', 'params': {}})
        row = activity['result']['sessions'][0]
        assert row['pending_inputs'] == [{'id': frame['id'], 'method': method}]
        assert row['status'] == 'waiting'
        for verb in ('session.resume', 'session.events.since'):
            reply = await owner.dispatch({'id': 2, 'method': verb, 'params': {'session_id': 's'}})
            replay = reply['result']['open_requests']
            assert replay[0]['id'] == frame['id']
            assert replay[0]['method'] == method
            expected = dict(frame['params'])
            if method == 'clarify':
                expected['answers'] = {'q0': 'blue'}
            assert replay[0]['params'] == expected
            replay[0]['params']['session_id'] = 'mutated'
        # A reader who never attached cannot recover an answerable request, even with the same session id.
        denied = await outsider.dispatch({'id': 3, 'method': 'session.events.since', 'params': {'session_id': 's'}})
        assert denied['result']['open_requests'] == []
        reader = replace(owner.actor, capabilities=owner.actor.capabilities - {'session:respond'})
        assert requests.open_requests(authority, reader, 's') == []
        assert requests.resolve({'id': frame['id'], 'result': {'value': 'entered-secret'}},
            session_ok=lambda sid, stream: requests.may_settle(authority, owner.actor, sid, stream))
        await task
        assert requests.open_requests(authority, owner.actor, 's') == []
        assert 'entered-secret' not in repr(peer.frames)
    finally:
        cancelled.set()
        await task
        await owner.close()
        await outsider.close()


@pytest.mark.asyncio
async def test_withdrawn_and_foreign_stream_requests_are_not_replayed(tmp_path, monkeypatch):
    from gateway import session_server_requests as requests
    from gateway.session_controls import AuthorityConnection
    from gateway.session_events import SessionEvents

    authority = await _authority(tmp_path, monkeypatch, lambda event: None)
    peer = Peer()
    owner = AuthorityConnection(authority, peer, {'user_id': 'owner'})
    await owner.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': 's'}})
    events = authority.sessions['s'].event_stream
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(requests.ask_viewer, events, 's', 'sudo',
        {'command': 'sudo true'}, cancelled=cancelled.is_set))
    try:
        await async_wait_until(lambda: any(frame.get('method') == 'sudo' for frame in peer.frames))
        assert requests.open_requests(authority, owner.actor, 's')
        original = events
        authority.sessions['s'].event_stream = SessionEvents()
        assert requests.open_requests(authority, owner.actor, 's') == []
        authority.sessions['s'].event_stream = original
        assert requests.withdraw(events, 's', 'sudo') == 1
        assert requests.open_requests(authority, owner.actor, 's') == []
    finally:
        cancelled.set()
        await task
        await owner.close()
