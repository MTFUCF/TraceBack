from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.parse import parse_qsl, unquote

from fastapi import FastAPI, Header, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field

from .auth import AuthenticationError, bearer_token_from_header
from .config import ConfigurationError, ServerConfig
from .graph import GraphToolService, ToolContext

if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor()


# Correlation headers whose values are non-secret and always captured when present.
DEFAULT_PLATFORM_ID_HEADERS = (
    "traceparent",
    "x-ms-client-request-id",
    "x-ms-correlation-request-id",
    "x-ms-correlation-id",
    "x-correlation-id",
    "x-request-id",
    "x-ms-request-id",
    "request-id",
)

# W3C baggage-format headers whose ID-shaped entries are captured so app-only calls
# can be bound to the platform conversation/response that caused them.
BAGGAGE_HEADERS = ("baggage", "x-ms-foundry-baggage", "correlation-context")
_BAGGAGE_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_BAGGAGE_ID_VALUE = re.compile(r"^[A-Za-z0-9._:@/+=-]{1,200}$")
_SENSITIVE_BAGGAGE_KEY = re.compile(r"token|secret|password|credential|assertion|authorization|cookie|key$", re.IGNORECASE)
MAX_BAGGAGE_ENTRIES = 32


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


def create_app(config: ServerConfig | None = None, service: GraphToolService | None = None) -> FastAPI:
    resolved_config = config or ServerConfig.from_environment()
    graph_service = service or GraphToolService(resolved_config)
    app = FastAPI(title="TraceBack Test-Rig MCP Server", version="0.1.0")
    if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        _add_application_insights_request_middleware(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"ready": "true", "auth_mode": resolved_config.auth_mode.value}

    @app.post("/mcp")
    async def mcp_rpc(payload: JsonRpcRequest, request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        try:
            if payload.method == "tools/list":
                result = {"tools": _tool_descriptions()}
            elif payload.method == "initialize":
                result = {
                    "protocolVersion": payload.params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "traceback-test-rig-mcp-server", "version": "0.1.0"},
                }
            elif payload.method == "notifications/initialized":
                result = {}
            elif payload.method == "tools/call":
                tool_result = _call_tool(graph_service, resolved_config, payload.params, request, authorization)
                result = {"content": [{"type": "text", "text": json.dumps(tool_result, separators=(",", ":"))}]}
            else:
                return _json_rpc_error(payload.id, -32601, "method_not_found")
            return {"jsonrpc": "2.0", "id": payload.id, "result": result}
        except (AuthenticationError, ConfigurationError, ValueError) as exc:
            return _json_rpc_error(payload.id, -32000, str(exc))

    @app.post("/tools/{tool_name}")
    async def tool_endpoint(tool_name: str, body: dict[str, Any], request: Request, authorization: str | None = Header(default=None)) -> Any:
        try:
            return _invoke_tool(graph_service, resolved_config, tool_name, body, request, authorization)
        except (AuthenticationError, ConfigurationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def _add_application_insights_request_middleware(app: FastAPI) -> None:
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    values = dict(parse_qsl(connection_string.replace(";", "&"), keep_blank_values=True))
    instrumentation_key = values.get("InstrumentationKey")
    if not instrumentation_key:
        return

    from applicationinsights import TelemetryClient

    client = TelemetryClient(instrumentation_key)

    class RequestTelemetryMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            start = time.perf_counter()
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000
            client.track_request(
                name=f"{request.method} {request.url.path}",
                url=str(request.url.replace(query="")),
                success=response.status_code < 500,
                start_time=None,
                duration=duration_ms,
                response_code=str(response.status_code),
                http_method=request.method,
            )
            client.flush()
            return response

    app.add_middleware(RequestTelemetryMiddleware)


def _call_tool(service: GraphToolService, config: ServerConfig, params: dict[str, Any], request: Request, authorization: str | None) -> Any:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise ValueError("tools/call requires string name and object arguments.")
    return _invoke_tool(service, config, name, arguments, request, authorization, params.get("_meta"))


def _invoke_tool(service: GraphToolService, config: ServerConfig, name: str, arguments: dict[str, Any], request: Request, authorization: str | None, meta: Any = None) -> Any:
    context = ToolContext(
        incoming_user_token=bearer_token_from_header(authorization),
        platform_ids=_platform_ids(config, request),
        inbound_header_names=_inbound_header_names(request),
        platform_baggage=_platform_baggage(request, meta),
    )
    if name == "get_user_profile":
        return service.get_user_profile(context, arguments.get("user_id_or_upn"))
    if name == "get_user_by_upn":
        return service.get_user_by_upn(context, str(arguments.get("upn") or ""))
    if name == "list_recent_signins":
        return service.list_recent_signins(context, int(arguments.get("top", 10)), arguments.get("user_principal_name"))
    raise ValueError(f"Unknown tool: {name}")


def _platform_ids(config: ServerConfig, request: Request) -> dict[str, str]:
    ids: dict[str, str] = {}
    for header_name in (*DEFAULT_PLATFORM_ID_HEADERS, *config.platform_id_headers):
        value = request.headers.get(header_name)
        if value and len(value) <= 512 and value.isprintable():
            ids[header_name] = value
    return ids


def _parse_baggage(raw: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for member in raw.split(","):
        key, sep, rest = member.partition("=")
        key = key.strip()
        if not sep or not _BAGGAGE_KEY.match(key) or len(entries) >= MAX_BAGGAGE_ENTRIES:
            continue
        entries[key] = _baggage_value(key, unquote(rest.split(";", 1)[0].strip()))
    return entries


def _baggage_value(key: str, value: str) -> str:
    # Only identifier-shaped values are kept; anything else is reduced to its length.
    if _SENSITIVE_BAGGAGE_KEY.search(key):
        return "<redacted>"
    if _BAGGAGE_ID_VALUE.match(value):
        return value
    return f"<omitted:len={len(value)}>"


def _platform_baggage(request: Request, meta: Any = None) -> dict[str, dict[str, str]]:
    captured: dict[str, dict[str, str]] = {}
    for header_name in BAGGAGE_HEADERS:
        value = request.headers.get(header_name)
        if value and len(value) <= 8192 and value.isprintable():
            if entries := _parse_baggage(value):
                captured[header_name] = entries
    if isinstance(meta, dict):
        entries = {
            str(k): _baggage_value(str(k), v) for k, v in list(meta.items())[:MAX_BAGGAGE_ENTRIES]
            if isinstance(v, str) and _BAGGAGE_KEY.match(str(k))
        }
        if entries:
            captured["mcp_meta"] = entries
    return captured


def _inbound_header_names(request: Request) -> tuple[str, ...]:
    # Names only: lets operators discover which correlation headers the caller sends.
    return tuple(sorted({name.lower() for name in request.headers.keys()}))[:64]


def _json_rpc_error(identifier: str | int | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _tool_descriptions() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_user_profile",
            "description": "GET the signed-in user's /me profile in delegated mode, or a specified user in application mode.",
            "inputSchema": {"type": "object", "properties": {"user_id_or_upn": {"type": "string"}}},
        },
        {
            "name": "get_user_by_upn",
            "description": "GET /users/{upn} from Microsoft Graph.",
            "inputSchema": {"type": "object", "required": ["upn"], "properties": {"upn": {"type": "string"}}},
        },
        {
            "name": "list_recent_signins",
            "description": "GET recent Entra sign-in records from Microsoft Graph auditLogs/signIns.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "top": {"type": "integer", "minimum": 1, "maximum": 50},
                    "user_principal_name": {"type": "string"},
                },
            },
        },
    ]


app = create_app()
