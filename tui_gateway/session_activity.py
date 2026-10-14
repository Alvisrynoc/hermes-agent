"""Read-only live activity snapshots shared by connected conversation observers."""

from .method_ctx import bind_module


def _session_pending_kind(sid: str) -> str:
    """Method of the server→client request *sid* is blocked on ("" when none)."""
    from tui_gateway import server_requests
    return server_requests.pending_kind(sid)


def _session_live_status(sid: str, session: dict) -> str:
    if _session_pending_kind(sid):
        return "waiting"
    ready = session.get("agent_ready")
    # Unset + build never started = a lazy watch session idling, not one stuck mid-construction.
    if ready is not None and not ready.is_set() and session.get("agent_build_started"):
        return "starting"
    return "working" if session.get("running") else "idle"


def _session_live_title(session: dict, key: str) -> str:
    title = str(session.get("pending_title") or "").strip()
    with contextlib.suppress(Exception), _session_db(session) as db:
        title = str(db.get_session_title(key) or title or "").strip() if db is not None else title
    return title


def _session_live_item(sid: str, session: dict, current_sid: str = "") -> dict:
    key = _session_lookup_key(session, fallback=sid)
    agent = session.get("agent")
    history = list(session.get("history") or [])
    status = _session_live_status(sid, session)
    inflight = _inflight_snapshot(session)
    queued = _queued_prompt_snapshot(session)
    preview = next((" ".join(text.split())[:160] for msg in reversed(history)
                    if (text := _content_display_text(msg.get("content", msg.get("text", ""))).strip())), "")
    if queued:
        preview = " ".join(str(queued.get("user") or preview).split())[:160]
    elif inflight:
        preview = " ".join(str(inflight.get("assistant") or inflight.get("user") or preview).split())[:160]
    from tui_gateway import server_requests

    with _session_home_scope(session):
        subagent_count = len(_visible_subagent_records(sid, session.get("transport"), session))
    now = time.time()
    return {
        "current": sid == current_sid, "id": sid,
        "last_active": float(session.get("last_active") or session.get("created_at") or now),
        "message_count": len(history),
        "model": str(getattr(agent, "model", "") or _resolve_model()), "preview": preview,
        "session_key": key, "started_at": float(session.get("created_at") or now), "status": status,
        "title": _session_live_title(session, key),
        "profile": profile_name_for_home(session.get("profile_home") or get_process_hermes_home()),
        "pending_inputs": server_requests.pending_inputs(sid),
        "active_subagent_count": subagent_count,
    }


def register(server):
    bind_module(globals(), server)
