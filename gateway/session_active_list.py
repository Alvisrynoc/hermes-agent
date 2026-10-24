"""``session.active_list`` on the session authority: the live state of the turns it owns.

Desktop's silent-turn check (``session-states.ts`` ``onEventSilence``) and its sidebar poll read
this verb. Unregistered here, the WS fell back to the legacy sidecar's handler, which lists only
the sidecar's own ``_sessions`` and never an authority-owned session: a foreground tool quiet for
45 s read as a reaped turn, and Desktop painted "Hermes ended this turn without a reply" over a
turn that was still running. Rows keep ``server._session_live_item``'s shape; ``id`` and
``session_key`` are both the session id, as ``session.resume`` reports them.
"""
from __future__ import annotations

import asyncio

from hermes_state_runtime import RuntimeStoreError


def _status(handle, pending, prompts) -> str:
    if prompts:
        return 'waiting'
    if handle.execution_state == 'running':
        return 'working'
    # Admitted, not yet claimed by the drain: the turn exists, its agent has not started.
    return 'starting' if any(row['status'] == 'queued' for row in pending) else 'idle'


def _subagent_counts(authority, entries):
    from gateway.session_ancillary import _owner_objects
    from gateway.session_managed_worker import managed_policy
    from tools.delegate_tool_registry import list_active_subagents

    counts = {}
    has_children = bool(list_active_subagents())
    for entry in entries:
        if authority.sessions.get(entry.session_id) is not entry.live:
            continue
        ref = entry.handle.ref
        try:
            if managed_policy(authority, ref) is not None:
                counts[entry.session_id] = None
            elif not has_children:
                counts[entry.session_id] = 0
            else:
                counts[entry.session_id] = len(_owner_objects(authority, ref, include_fallback_evicted=True)[1])
        except KeyError:
            if authority.sessions.get(entry.session_id) is entry.live:
                raise
    return counts


async def active_list(connection, ref, params):
    if set(params) - {'profile', 'current_session_id'}:
        raise RuntimeStoreError('invalid_params')
    if 'session:read' not in connection.actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    from gateway.session_live_listing import live_entries
    from gateway.session_authorities import served_profile_name
    from gateway.session_server_requests import pending_inputs
    from hermes_state_sessions import INTERNAL_LISTING_SOURCES
    current = str(params.get('current_session_id') or '')
    rows = []
    entries = await live_entries(connection.authority, connection.actor)
    # Conversation lineage may read the store; keep those reads off the gateway event loop.
    counts = await asyncio.to_thread(_subagent_counts, connection.authority, entries)
    for entry in entries:
        if (entry.session_id not in counts or
                connection.authority.sessions.get(entry.session_id) is not entry.live):
            continue
        row = entry.row
        # The same population as the live ``session.list`` feed, hidden chats included: a hidden
        # chat open in a pane still needs its turn's liveness.
        if row.get('source') in INTERNAL_LISTING_SOURCES or entry.closed:
            continue
        sid, started = entry.session_id, float(row.get('started_at') or 0)
        prompts = entry.live.controls.snapshot(sid, entry.handle.execution_generation)
        inputs = [{'id': prompt['prompt_id'], 'method': prompt['kind']} for prompt in prompts]
        inputs.extend(pending_inputs(entry.live.event_stream, sid))
        rows.append({'current': sid == current, 'id': sid, 'session_key': sid,
                     'last_active': float(row.get('ended_at') or started), 'started_at': started,
                     'message_count': int(row.get('message_count') or 0),
                     'model': str(row.get('model') or ''), 'preview': '', 'title': row.get('title') or '',
                     'source': row.get('source'), 'status': _status(entry.handle, entry.pending, inputs),
                     'profile': served_profile_name(entry.handle.ref.profile_id),
                     'pending_inputs': inputs,
                     'active_subagent_count': counts[sid]})
    return {'sessions': rows}
