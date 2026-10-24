"""``session.active_list`` is answered by the session authority from its own live turns.

Desktop's silent-turn check (``session-states.ts`` ``onEventSilence``) asks this verb whether a
turn that went 45 s without an event is still running. The authority never registered it, so the
WS fell back to the legacy sidecar's handler, whose ``_sessions`` never holds an authority-owned
session: a foreground tool quiet for 45 s read as a reaped turn, and Desktop painted "Hermes ended
this turn without a reply" over a turn that was still running.
"""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def owner(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    from gateway.session_authority import LiveSession, initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    started, release = asyncio.Event(), asyncio.Event()

    async def answer(event):
        # A long foreground tool: the turn emits nothing until it returns.
        started.set()
        await release.wait()
        return event.text

    runner = SimpleNamespace(_session_db=store._db, session_store=store, _draining=False,
                             _handle_message=answer, _adapter_for_source=lambda source: None)
    authority = await initialize_session_authority(runner, profile_id='live', instance_id='owner')
    store._db.create_session('chat', source='telegram')
    authority.sessions['chat'] = LiveSession(SessionSource(platform=Platform.TELEGRAM, chat_id='chat'), 'chat')
    connection = AuthorityConnection(authority, object(), {'user_id': 'viewer'})

    async def active(**params):
        reply = await connection.dispatch({'id': 1, 'method': 'session.active_list', 'params': params})
        assert 'result' in reply, reply
        return {row['id']: row for row in reply['result']['sessions']}

    async def submit(text):
        from gateway.session_contract import SessionRef, Submission
        await authority.submit(connection.actor, Submission(text, SessionRef('live', 'chat'), {'text': text}, 'queue'))
        await asyncio.wait_for(started.wait(), 5)

    yield SimpleNamespace(authority=authority, active=active, submit=submit, release=release)
    release.set()


@pytest.mark.asyncio
async def test_a_running_turn_reads_working_until_it_settles(owner):
    assert (await owner.active())['chat']['status'] == 'idle'
    await owner.submit('hi')
    row = (await owner.active(current_session_id='chat'))['chat']
    assert (row['status'], row['session_key'], row['current']) == ('working', 'chat', True), row
    owner.release.set()
    await owner.authority.sessions['chat'].task
    assert (await owner.active())['chat']['status'] == 'idle'


@pytest.mark.asyncio
async def test_a_row_names_its_source(owner):
    # Desktop's poll can see a messaging turn settle before its messaging row loads; the source
    # on the live row is what keeps that settle from reading as a Desktop completion.
    assert (await owner.active())['chat']['source'] == 'telegram'


@pytest.mark.asyncio
async def test_a_turn_blocked_on_a_prompt_reads_waiting(owner, monkeypatch):
    await owner.submit('hi')
    controls = owner.authority.sessions['chat'].controls
    monkeypatch.setattr(controls, 'snapshot', lambda session_id, generation: ({'kind': 'approval', 'prompt_id': 'approve'},))
    assert (await owner.active())['chat']['status'] == 'waiting'


@pytest.mark.asyncio
async def test_observer_projects_native_prompt_ids_without_sensitive_bodies(owner):
    import json
    from gateway.session_authorities import served_profile_name

    await owner.submit('hi')
    live = owner.authority.sessions['chat']
    # Remote native prompts remain active until their registered responder settles them.
    from gateway.session_contract import SessionRef
    generation = owner.authority._handle(SessionRef('live', 'chat')).execution_generation
    for kind in ('approval', 'clarify'):
        prompt_id = f'native-{kind}'
        live.controls.pending[prompt_id] = (None, {
            'kind': kind, 'prompt_id': prompt_id, 'execution_generation': generation,
            'command': 'sensitive command', 'question': 'private question',
        })
        live.controls.remote_responders[prompt_id] = lambda response: None
    row = (await owner.active())['chat']
    assert row['profile'] == served_profile_name(owner.authority.profile_id)
    assert row['pending_inputs'] == [{'id': 'native-approval', 'method': 'approval'},
                                     {'id': 'native-clarify', 'method': 'clarify'}]
    assert 'sensitive command' not in json.dumps(row)
    assert 'private question' not in json.dumps(row)
    live.controls.pending.clear()
    live.controls.remote_responders.clear()
    assert (await owner.active())['chat']['pending_inputs'] == []


@pytest.mark.asyncio
async def test_observer_counts_only_its_conversation_children(owner, monkeypatch):
    from tools import delegate_tool_registry as registry

    child = SimpleNamespace(_session_db=owner.authority.db)
    monkeypatch.setattr(registry, '_active_subagents', {
        'own': {'agent': child, 'owner_agent_session_id': 'chat'},
        'foreign': {'agent': child, 'owner_agent_session_id': 'other'},
    })
    monkeypatch.setattr(owner.authority.runner, '_cached_agent_for', lambda route: None, raising=False)
    assert (await owner.active())['chat']['active_subagent_count'] == 1
    registry._active_subagents.clear()
    assert (await owner.active())['chat']['active_subagent_count'] == 0


@pytest.mark.asyncio
async def test_managed_worker_observer_count_is_unknown(owner, monkeypatch):
    from gateway import session_managed_worker

    monkeypatch.setattr(session_managed_worker, 'managed_policy', lambda authority, ref: object())
    assert (await owner.active())['chat']['active_subagent_count'] is None


@pytest.mark.asyncio
async def test_a_session_retired_during_roster_read_leaves_the_snapshot(owner, monkeypatch):
    from gateway import session_active_list

    original = session_active_list._subagent_counts

    def retire_before_count(authority, entries):
        authority.sessions.pop('chat')
        return original(authority, entries)

    monkeypatch.setattr(session_active_list, '_subagent_counts', retire_before_count)
    assert await owner.active() == {}
