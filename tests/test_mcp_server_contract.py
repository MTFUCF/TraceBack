import base64
import json
from pathlib import Path
import sys

import pytest


MCP_ROOT = Path(__file__).resolve().parents[1] / "testrig" / "mcp-server"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from tb_mcp_server.audit import GraphAuditEvent, format_audit_event
from tb_mcp_server.config import AuthMode, ConfigurationError, ServerConfig
from tb_mcp_server.auth import AccessTokenResult
from tb_mcp_server.graph import GraphToolService, ToolContext
from tb_mcp_server.token_claims import safe_token_claims


def test_delegated_auth_mode_requires_obo_configuration():
    config = ServerConfig.from_environment({
        "TB_AUTH_MODE": "delegated",
        "TB_GRAPH_SCOPES": "https://graph.microsoft.com/.default",
        "TB_TENANT_ID": "tenant-placeholder",
        "TB_CLIENT_ID": "client-placeholder",
        "TB_CLIENT_SECRET": "secret-placeholder",
    })

    assert config.auth_mode is AuthMode.DELEGATED
    assert config.graph_scopes == ("https://graph.microsoft.com/.default",)


def test_application_auth_mode_can_use_default_azure_credential():
    config = ServerConfig.from_environment({
        "TB_AUTH_MODE": "application",
        "TB_GRAPH_SCOPES": "https://graph.microsoft.com/.default",
    })

    assert config.auth_mode is AuthMode.APPLICATION
    assert config.client_secret is None


def test_invalid_or_missing_auth_mode_fails_closed():
    with pytest.raises(ConfigurationError) as caught:
        ServerConfig.from_environment({"TB_GRAPH_SCOPES": "scope"})

    assert caught.value.reason == "missing_tb_auth_mode"

    with pytest.raises(ConfigurationError):
        ServerConfig.from_environment({"TB_AUTH_MODE": "unsafe", "TB_GRAPH_SCOPES": "scope"})


def test_audit_log_line_schema_is_single_line_json():
    line = format_audit_event(GraphAuditEvent(
        timestamp_utc="2026-09-19T17:10:00Z",
        tool_name="get_user_by_upn",
        auth_mode="application",
        graph_request_uri="https://graph.microsoft.com/v1.0/users/example",
        http_status=200,
        graph_client_request_id="graph-client-request",
        graph_request_id="graph-request",
        token_claims={"appid": "application-id", "tid": "tenant-id", "roles": ["User.Read.All"]},
        platform_ids={"x-ms-foundry-thread-id": "thread-from-platform"},
    ))

    assert "\n" not in line
    payload = json.loads(line)
    assert payload == {
        "schema_version": 3,
        "timestamp_utc": "2026-09-19T17:10:00Z",
        "event": "graph_request",
        "tool_name": "get_user_by_upn",
        "auth_mode": "application",
        "graph_request_uri": "https://graph.microsoft.com/v1.0/users/example",
        "http_status": 200,
        "graph_client_request_id": "graph-client-request",
        "graph_request_id": "graph-request",
        "token_claims": {"appid": "application-id", "tid": "tenant-id", "roles": ["User.Read.All"]},
        "platform_ids": {"x-ms-foundry-thread-id": "thread-from-platform"},
        "outbound_token_source": None,
        "outbound_token_claims": {},
        "inbound_header_names": [],
        "platform_baggage": {},
    }


def test_secret_and_token_material_cannot_reach_log_formatter():
    token = _unsigned_jwt({"oid": "user-object", "tid": "tenant-id", "email": "person@example.invalid"})
    line = format_audit_event(GraphAuditEvent(
        tool_name="get_user_profile",
        auth_mode="delegated",
        graph_request_uri="https://graph.microsoft.com/v1.0/me",
        http_status=200,
        token_claims={
            **safe_token_claims(token),
            "access_token": token,
            "Authorization": f"Bearer {token}",
            "client_secret": "plain-secret",
        },
    ))

    assert token not in line
    assert "plain-secret" not in line
    assert "Bearer " not in line
    assert "person@example.invalid" not in line
    payload = json.loads(line)
    assert payload["token_claims"]["oid"] == "user-object"
    assert payload["token_claims"]["access_token"] == "<redacted>"
    assert payload["token_claims"]["Authorization"] == "<redacted>"
    assert payload["token_claims"]["client_secret"] == "<redacted>"


class _FakeTokenProvider:
    def __init__(self, token):
        self.token = token

    def get_graph_token(self, incoming_user_token):
        return AccessTokenResult(self.token, "obo")


class _FakeResponse:
    status_code = 200
    headers = {"request-id": "graph-server-request"}

    def raise_for_status(self):
        return None

    def json(self):
        return {"id": "target-object"}


class _FakeHttpClient:
    def __init__(self):
        self.calls = []

    def get(self, url, headers):
        self.calls.append((url, headers))
        return _FakeResponse()


def test_graph_call_sends_caller_chosen_client_request_id_and_logs_token_binding(capsys):
    config = ServerConfig.from_environment({
        "TB_AUTH_MODE": "delegated",
        "TB_GRAPH_SCOPES": "User.ReadBasic.All",
        "TB_TENANT_ID": "tenant-placeholder",
        "TB_CLIENT_ID": "client-placeholder",
    })
    outbound = _unsigned_jwt({"oid": "user-object", "appid": "mcp-app", "uti": "outbound-uti", "idtyp": "user", "email": "person@example.invalid"})
    inbound = _unsigned_jwt({"oid": "user-object", "appid": "caller-app", "uti": "inbound-uti"})
    http = _FakeHttpClient()
    service = GraphToolService(config, token_provider=_FakeTokenProvider(outbound), http_client=http)

    service.get_user_by_upn(ToolContext(
        incoming_user_token=inbound,
        platform_ids={"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"},
        inbound_header_names=("authorization", "traceparent"),
    ), "Target@example.invalid")

    (_, headers), = http.calls
    sent = headers["client-request-id"]
    assert len(sent) == 36 and headers["return-client-request-id"] == "true"
    line = capsys.readouterr().out.strip()
    payload = json.loads(line)
    assert payload["graph_client_request_id"] == sent
    assert payload["graph_request_id"] == "graph-server-request"
    assert payload["token_claims"]["uti"] == "inbound-uti"
    assert payload["outbound_token_source"] == "obo"
    assert payload["outbound_token_claims"] == {"oid": "user-object", "appid": "mcp-app", "uti": "outbound-uti", "idtyp": "user"}
    assert payload["inbound_header_names"] == ["authorization", "traceparent"]
    assert outbound not in line and inbound not in line
    assert "person@example.invalid" not in line


def test_each_graph_call_gets_a_distinct_client_request_id(capsys):
    config = ServerConfig.from_environment({"TB_AUTH_MODE": "application", "TB_GRAPH_SCOPES": "https://graph.microsoft.com/.default"})
    http = _FakeHttpClient()
    service = GraphToolService(config, token_provider=_FakeTokenProvider("opaque"), http_client=http)

    service.get_user_by_upn(ToolContext(), "a@example.invalid")
    service.get_user_by_upn(ToolContext(), "a@example.invalid")

    assert http.calls[0][1]["client-request-id"] != http.calls[1][1]["client-request-id"]


def test_server_captures_default_correlation_headers_and_header_names_only(monkeypatch):
    monkeypatch.setenv("TB_AUTH_MODE", "application")
    monkeypatch.setenv("TB_GRAPH_SCOPES", "https://graph.microsoft.com/.default")
    from starlette.requests import Request
    from tb_mcp_server.server import _inbound_header_names, _platform_ids

    request = Request({"type": "http", "headers": [
        (b"traceparent", b"00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"),
        (b"authorization", b"Bearer secret-token"),
        (b"x-ms-client-request-id", b"platform-request"),
        (b"x-custom", b"line\nbreak"),
    ]})
    config = ServerConfig.from_environment({"TB_AUTH_MODE": "application", "TB_GRAPH_SCOPES": "scope", "TB_PLATFORM_ID_HEADERS": "x-custom"})

    ids = _platform_ids(config, request)

    assert ids["traceparent"].startswith("00-0af7651916cd43dd8448eb211c80319c")
    assert ids["x-ms-client-request-id"] == "platform-request"
    assert "authorization" not in ids
    assert _inbound_header_names(request) == ("authorization", "traceparent", "x-custom", "x-ms-client-request-id")


def test_baggage_headers_keep_identifier_values_and_drop_free_text_and_secrets():
    from starlette.requests import Request
    from tb_mcp_server.server import _platform_baggage

    request = Request({"type": "http", "headers": [
        (b"baggage", b"conversation_id=conv_abc123,user.prompt=hello%20there%20world,session.token=abc;prop=1"),
        (b"x-ms-foundry-baggage", b"response_id=resp_42, agent.name=tb-agent-apponly-rig, bad key=x"),
    ]})
    meta = {"progressToken": "p-1", "foundry.thread": "thread_9", "nested": {"x": 1}}

    captured = _platform_baggage(request, meta)

    assert captured["baggage"] == {
        "conversation_id": "conv_abc123",
        "user.prompt": "<omitted:len=17>",
        "session.token": "<redacted>",
    }
    assert captured["x-ms-foundry-baggage"] == {
        "response_id": "resp_42", "agent.name": "tb-agent-apponly-rig",
    }
    assert captured["mcp_meta"] == {"progressToken": "<redacted>", "foundry.thread": "thread_9"}


def _unsigned_jwt(payload):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"
