"""MCP framing, API contracts and dedicated credential boundaries."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from agent_tools import AcquisitionTools, CredentialError, PROTOCOL_VERSION, ProtocolError, TOOLS, load_credentials, serve


@pytest.fixture
def bridge():
    received = []

    def responder(request):
        received.append(request)
        return httpx.Response(200, json={"ok": True, "stage": "review"})

    instance = AcquisitionTools("https://acquire.example", "private-token", transport=httpx.MockTransport(responder))
    yield instance, received
    instance.close()


def initialize(instance):
    response = instance.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
    assert response["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert instance.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


@pytest.mark.parametrize("name,args,method,path,payload", [
    ("request_music", {"kind": "link", "url": "https://youtu.be/example", "link_kind": "album"}, "POST", "/api/requests", {"kind": "link", "url": "https://youtu.be/example", "link_kind": "album", "hold": True}),
    ("request_music", {"kind": "album", "artist": "Moor Mother", "album": "Jazz Codes"}, "POST", "/api/requests", {"kind": "album", "artist": "Moor Mother", "album": "Jazz Codes", "hold": True}),
    ("request_music", {"kind": "track", "artist": "Vulfpeck", "title": "Sauna", "hold": False}, "POST", "/api/requests", {"kind": "track", "artist": "Vulfpeck", "title": "Sauna", "hold": False}),
    ("requests", {}, "GET", "/api/requests", {"status": "active", "limit": 50}),
    ("requests", {"q": "blawan", "status": "all", "limit": 5}, "GET", "/api/requests", {"q": "blawan", "status": "all", "limit": 5}),
    ("requests", {"id": "r"}, "GET", "/api/requests/r", {"status": "active", "limit": 50}),
    ("request_action", {"id": "r", "action": "choose", "candidate_id": "c"}, "POST", "/api/requests/r/choose", {"candidate_id": "c"}),
    ("request_action", {"id": "r", "action": "retry"}, "POST", "/api/requests/r/retry", {}),
    ("request_action", {"id": "r", "action": "skip"}, "POST", "/api/requests/r/skip", {}),
    ("request_action", {"id": "r", "action": "remove"}, "POST", "/api/requests/r/remove", {}),
    ("pending", {}, "GET", "/api/pending", {}),
    ("decide_review", {"id": "job", "action": "add", "file_ids": ["f1"], "keep_existing": True}, "POST", "/api/jobs/job/approve", {"selected_file_ids": ["f1"], "keep_existing": True}),
    ("decide_review", {"id": "job", "action": "add"}, "POST", "/api/jobs/job/approve", {}),
    ("decide_review", {"id": "job", "action": "skip", "file_ids": ["f1"]}, "POST", "/api/jobs/job/skip", {"file_ids": ["f1"]}),
    ("decide_review", {"id": "job", "action": "reject"}, "POST", "/api/jobs/job/reject", {}),
    ("answer_check", {"username": "peer", "message": "watermelon"}, "POST", "/api/soulseek/checks/peer/reply", {"message": "watermelon"}),
    ("library", {"q": "Sauna", "genre": "Funk", "key": "Cm", "bpm_min": 90, "bpm_max": 110, "page": 2, "sort": "album"}, "GET", "/api/library", {"q": "Sauna", "genre": "Funk", "key": "Cm", "bpm_min": "90", "bpm_max": "110", "page": "2", "sort": "album"}),
    ("library_action", {"action": "unshare", "file_ids": ["f1", "f2"]}, "POST", "/api/library/unshare", {"file_ids": ["f1", "f2"]}),
    ("library_action", {"action": "delete", "file_ids": ["f1"]}, "POST", "/api/library/delete", {"file_ids": ["f1"]}),
    ("request_approval_token", {"name": "reviewer", "can_approve": True}, "POST", "/api/agents/requests", {"name": "reviewer", "can_approve": True}),
    ("claim_approval_token", {"id": "abc"}, "GET", "/api/agents/requests/abc", {}),
])
def test_tools_use_exact_api_contract(bridge, name, args, method, path, payload):
    instance, received = bridge
    result = instance.call(name, args)
    assert result["isError"] is False
    assert result["structuredContent"]["stage"] == "review"
    request = received[0]
    assert request.method == method
    assert request.url.path == path
    assert request.headers["authorization"] == "Bearer private-token"
    if method == "POST":
        assert json.loads(request.content) == payload
    else:
        assert dict(request.url.params) == {k: str(v) for k, v in payload.items()}


@pytest.mark.parametrize("name,args", [
    ("enqueue", {"candidate_id": "x"}), ("request_music", {"candidate_id": "x"}), ("request_music", {"kind": "album", "approve": True}),
    ("request_music", {"kind": "playlist"}), ("request_music", {"hold": "yes"}),
    ("requests", {"status": "wrong"}), ("requests", {"limit": 0}), ("requests", {"id": ""}),
    ("request_action", {"id": "r"}), ("request_action", {"id": "r", "action": "cancel"}),
    ("decide_review", {"id": "j"}), ("decide_review", {"id": "j", "action": "approve"}), ("decide_review", {"id": "j", "action": "add", "keep_existing": "yes"}),
    ("decide_review", {"id": "j", "action": "skip", "file_ids": []}), ("decide_review", {"id": "j", "action": "add", "files": []}),
    ("answer_check", {"username": "peer", "message": ""}), ("answer_check", {"username": "peer", "message": "x" * 201}),
    ("library_action", {"action": "erase", "file_ids": ["f1"]}), ("library_action", {"action": "remove"}), ("library_action", {"action": "remove", "file_ids": []}),
    ("library", {"page": True}), ("library", {"bpm_min": float("inf")}), ("library", {"page": 0}),
    ("health", {}), ("import_files", {"paths": ["x"]}), ("search_music", {"query": "x"}), ("get_job", {"id": "x"}), ("approve_review", {"id": "job"}),
])
def test_invalid_inputs_never_reach_http(bridge, name, args):
    instance, received = bridge
    with pytest.raises(ProtocolError) as failure:
        instance.call(name, args)
    assert failure.value.code == -32602
    assert received == []


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 409, 422, 429, 500, 503])
def test_http_errors_are_safe_tool_results(status):
    instance = AcquisitionTools("https://acquire.example", "private-token", transport=httpx.MockTransport(lambda _: httpx.Response(status, text='secret-error-body private-token')))
    try:
        result = instance.call("pending", {})
        assert result["isError"] is True
        assert "secret-error-body" not in json.dumps(result)
        assert "private-token" not in json.dumps(result)
    finally:
        instance.close()


def test_network_and_invalid_json_errors_are_sanitized():
    def unavailable(request):
        raise httpx.ConnectError("private-token secret-network-diagnostic", request=request)

    for transport in [httpx.MockTransport(unavailable), httpx.MockTransport(lambda _: httpx.Response(200, text='secret-invalid-json'))]:
        instance = AcquisitionTools("https://acquire.example", "private-token", transport=transport)
        try:
            result = instance.call("pending", {})
            assert result["isError"] is True
            assert "secret" not in json.dumps(result)
        finally:
            instance.close()


def test_echoed_bearer_is_redacted():
    instance = AcquisitionTools("https://acquire.example", "private-token", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"echo": "private-token"})))
    try:
        assert "private-token" not in json.dumps(instance.call("pending", {}))
    finally:
        instance.close()


def test_identifier_is_encoded_as_one_path_component(bridge):
    instance, received = bridge
    instance.call("request_action", {"id": "r/choose?x=1", "action": "skip"})
    assert received[0].url.raw_path == b"/api/requests/r%2Fchoose%3Fx%3D1/skip"
    instance.call("answer_check", {"username": "peer/one", "message": "watermelon"})
    assert received[1].url.raw_path == b"/api/soulseek/checks/peer%2Fone/reply"


def test_handshake_registry_notifications_and_invalid_requests(bridge):
    instance, received = bridge
    assert instance.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})["error"]["code"] == -32002
    initialize(instance)
    assert instance.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
    registry = instance.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
    assert len(registry) == 10
    assert [item["name"] for item in registry if "review" in item["name"]] == ["decide_review"]
    assert not any(any(word in item["name"] for word in ["publish", "delete", "sharing", "approve"]) for item in registry)
    for item in registry:
        assert "endpoint" not in item and "method" not in item
        assert item["inputSchema"]["additionalProperties"] is False
        expected = next(t["method"] == "GET" for t in TOOLS if t["name"] == item["name"])
        assert item["annotations"]["readOnlyHint"] is expected
    assert instance.handle({"jsonrpc": "2.0", "method": "notifications/unrecognized"}) is None
    assert instance.handle({"jsonrpc": "2.0", "id": 4, "method": "missing"})["error"]["code"] == -32601
    assert instance.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "approve"}})["error"]["code"] == -32602
    for message in [[], {"id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": True, "method": "ping"}, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}]:
        assert instance.handle(message)["error"]["code"] == -32600
    assert received == []


def test_credentials_private_file_validation(tmp_path):
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps({"base_url": "https://acquire.example/", "token": "private-token"}))
    path.chmod(0o600)
    assert load_credentials(path) == ("https://acquire.example", "private-token")
    path.chmod(0o644)
    with pytest.raises(CredentialError, match="0600"):
        load_credentials(path)
    path.chmod(0o600)
    for invalid in [{"base_url": "https://user:private-token@host", "token": "private-token"}, {"base_url": "https://host", "token": ""}, [], {"base_url": "file:///private-token", "token": "x"}]:
        path.write_text(json.dumps(invalid))
        with pytest.raises(CredentialError) as failure:
            load_credentials(path)
        assert "private-token" not in str(failure.value)


def test_stdio_protocol_subprocess_has_only_json_messages(tmp_path):
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps({"base_url": "http://127.0.0.1:1", "token": "private-token"}))
    path.chmod(0o600)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "registry", "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    script = Path(__file__).resolve().parents[1] / 'agent_tools.py'
    env = {**os.environ, "ACQUIRE_AGENT_CREDENTIALS": str(path)}
    response = subprocess.run([sys.executable, str(script)], input='\n'.join(json.dumps(m) for m in messages) + '\nnot-json\n', capture_output=True, text=True, env=env, timeout=10)
    assert response.returncode == 0
    lines = [json.loads(line) for line in response.stdout.splitlines()]
    assert [line["id"] for line in lines] == [1, "registry", 3, None]
    assert lines[1]["result"]["tools"]
    assert lines[-1]["error"]["code"] == -32700
    assert 'private-token' not in response.stdout + response.stderr
    assert response.stderr == ''


def test_protocol_parse_error_does_not_prevent_next_request(bridge):
    instance, _ = bridge
    output = io.StringIO()
    serve(instance, io.StringIO('NaN\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'), output)
    results = [json.loads(line) for line in output.getvalue().splitlines()]
    assert results[0]["error"]["code"] == -32700
    assert results[1]["result"] == {}


def test_a_claimed_token_is_saved_used_and_never_returned(tmp_path):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"base_url": "https://acquire.example", "token": "old-token"}))
    os.chmod(credentials, 0o600)
    received = []

    def responder(request):
        received.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"status": "allowed", "token": "new-token", "can_approve": True})
        return httpx.Response(200, json={"ok": True})

    instance = AcquisitionTools("https://acquire.example", "old-token", transport=httpx.MockTransport(responder), credentials=str(credentials))
    result = instance.call("claim_approval_token", {"id": "request"})
    assert result["isError"] is False and "new-token" not in json.dumps(result)
    import hashlib
    assert result["structuredContent"]["saved"] is True
    assert result["structuredContent"]["previous_token_id"] == hashlib.sha256(b"old-token").hexdigest()
    assert len(received) == 1   # the old token may be shared with another agent: never revoked here
    assert json.loads(credentials.read_text())["token"] == "new-token"
    assert os.stat(credentials).st_mode & 0o777 == 0o600
    load_credentials(credentials)
    instance.close()
