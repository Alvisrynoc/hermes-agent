"""Read-only live snapshots expose input identities without granting turn ownership."""

import json
from types import SimpleNamespace

import pytest

from tui_gateway import server, server_requests


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    server_requests.reset_for_tests()
    home = tmp_path / "profiles" / "research"
    home.mkdir(parents=True)
    owner_transport = SimpleNamespace(write=lambda frame: True)
    owner = {
        "profile_home": str(home), "session_key": "conversation", "history": [],
        "agent": SimpleNamespace(session_id="conversation", model="test"),
        "transport": owner_transport, "running": True,
    }
    monkeypatch.setattr(server, "_sessions", {"desktop": owner})
    monkeypatch.setattr(server, "_session_live_title", lambda session, key: "Research")
    server_requests.bind_sinks(lambda frame: None, lambda *args: None, lambda sid: True)
    yield owner, owner_transport
    server_requests.reset_for_tests()
    server_requests.bind_sinks(server.write_json, server._emit, server._session_client_answers_requests,
                              server._session_answering_clients)


@pytest.mark.parametrize("explicit_home", [True, False])
@pytest.mark.parametrize("method,params", [
    ("approval", {"request_id": "approval-queue-entry", "command": "sensitive command",
                  "description": "private instructions"}),
    ("clarify", {"questions": [{"qid": "q", "question": "private instructions"}]}),
])
def test_observer_gets_profile_and_open_input_identity_without_sensitive_body(
        runtime, monkeypatch, explicit_home, method, params):
    owner, transport = runtime
    if not explicit_home:
        launch_home = owner.pop("profile_home")
        monkeypatch.setattr(server, "get_process_hermes_home", lambda: launch_home)
    settle = server_requests.send_async(method, "desktop", params, lambda result: None)
    try:
        request = server_requests.open_requests("desktop")[0]
        row = server.handle_request({"id": 1, "method": "session.active_list", "params": {}})["result"]["sessions"][0]
        assert row["profile"] == "research"
        assert row["status"] == "waiting"
        assert row["pending_inputs"] == [{"id": request["id"], "method": method}]
        assert "sensitive command" not in json.dumps(row)
        assert "private instructions" not in json.dumps(row)
        assert owner["transport"] is transport
        settle("timeout")
        assert server._session_live_item("desktop", owner)["pending_inputs"] == []
    finally:
        settle("cancelled")
