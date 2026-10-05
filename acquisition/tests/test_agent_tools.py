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
    ("health", {}, "GET", "/api/health", {}),
    ("search_music", {"query": "Sauna", "source": "soulseek", "kind": "track", "artist": "Vulfpeck"}, "POST", "/api/search", {"query": "Sauna", "source": "soulseek", "kind": "track", "artist": "Vulfpeck"}),
    ("get_search", {"id": "search"}, "GET", "/api/search/search", {}),
    ("list_jobs", {}, "GET", "/api/jobs", {}),
    ("get_job", {"id": "job"}, "GET", "/api/jobs/job", {}),
    ("list_reviews", {}, "GET", "/api/review", {}),
    ("request_review_advice", {"id": "job"}, "POST", "/api/jobs/job/advice", {}),
    ("library", {"q": "Sauna", "genre": "Funk", "key": "Cm", "bpm_min": 90, "bpm_max": 110, "page": 2, "sort": "album"}, "GET", "/api/library", {"q": "Sauna", "genre": "Funk", "key": "Cm", "bpm_min": "90", "bpm_max": "110", "page": "2", "sort": "album"}),
    ("enqueue", {"candidate_id": "candidate", "artist": "Vulfpeck", "title": "Sauna", "album": "MSG II"}, "POST", "/api/jobs", {"candidate_id": "candidate", "artist": "Vulfpeck", "title": "Sauna", "album": "MSG II"}),
    ("enqueue_url", {"url": "https://youtu.be/example", "kind": "album"}, "POST", "/api/url", {"url": "https://youtu.be/example", "kind": "album"}),
    ("retry_job", {"id": "job", "stage": "processing"}, "POST", "/api/jobs/job/retry", {"stage": "processing"}),
    ("cancel_job", {"id": "job"}, "POST", "/api/jobs/job/cancel", {}),
    ("import_files", {"paths": ["Album/Sauna.m4a"]}, "POST", "/api/import", {"paths": ["Album/Sauna.m4a"]}),
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
    ("enqueue", {}), ("enqueue", {"candidate_id": 5}), ("enqueue", {"candidate_id": "x", "approve": True}),
    ("retry_job", {"id": "x", "stage": "publish"}), ("search_music", {"source": "unsupported"}),
    ("library", {"page": True}), ("library", {"bpm_min": float("inf")}), ("library", {"page": 0}),
    ("import_files", {"paths": []}), ("import_files", {"paths": [1]}), ("import_files", {"paths": ["x"] * 101}),
    ("health", []), ("approve", {"id": "job"}),
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
        result = instance.call("health", {})
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
            result = instance.call("health", {})
            assert result["isError"] is True
            assert "secret" not in json.dumps(result)
        finally:
            instance.close()


def test_echoed_bearer_is_redacted():
    instance = AcquisitionTools("https://acquire.example", "private-token", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"echo": "private-token"})))
    try:
        assert "private-token" not in json.dumps(instance.call("health", {}))
    finally:
        instance.close()


def test_identifier_is_encoded_as_one_path_component(bridge):
    instance, received = bridge
    instance.call("cancel_job", {"id": "job/approve?x=1"})
    assert received[0].url.raw_path == b"/api/jobs/job%2Fapprove%3Fx%3D1/cancel"


def test_handshake_registry_notifications_and_invalid_requests(bridge):
    instance, received = bridge
    assert instance.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})["error"]["code"] == -32002
    initialize(instance)
    assert instance.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
    registry = instance.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
    assert len(registry) == 13
    assert not any(any(word in item["name"] for word in ["approve", "reject", "publish", "delete", "sharing"]) for item in registry)
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
