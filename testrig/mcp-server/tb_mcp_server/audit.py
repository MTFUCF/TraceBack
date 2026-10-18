from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import re
from typing import Any


_SECRET_KEYS = re.compile(
    r"^(authorization|access_token|id_token|refresh_token|client_secret|secret|password|credential|assertion)$",
    re.IGNORECASE,
)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class GraphAuditEvent:
    tool_name: str
    auth_mode: str
    graph_request_uri: str
    http_status: int | None
    graph_client_request_id: str | None = None
    graph_request_id: str | None = None
    token_claims: dict[str, Any] = field(default_factory=dict)
    platform_ids: dict[str, str] = field(default_factory=dict)
    timestamp_utc: str | None = None
    outbound_token_source: str | None = None
    outbound_token_claims: dict[str, Any] = field(default_factory=dict)
    inbound_header_names: tuple[str, ...] = ()
    platform_baggage: dict[str, dict[str, str]] = field(default_factory=dict)


def format_audit_event(event: GraphAuditEvent) -> str:
    """Format one stdout JSON line and defensively redact unsafe values."""
    payload = {
        "schema_version": 3,
        "timestamp_utc": event.timestamp_utc or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "event": "graph_request",
        "tool_name": event.tool_name,
        "auth_mode": event.auth_mode,
        "graph_request_uri": event.graph_request_uri,
        "http_status": event.http_status,
        "graph_client_request_id": event.graph_client_request_id,
        "graph_request_id": event.graph_request_id,
        "token_claims": event.token_claims,
        "platform_ids": event.platform_ids,
        "outbound_token_source": event.outbound_token_source,
        "outbound_token_claims": event.outbound_token_claims,
        "inbound_header_names": list(event.inbound_header_names),
        "platform_baggage": event.platform_baggage,
    }
    return json.dumps(_sanitize(payload), sort_keys=True, separators=(",", ":"))


def emit_audit_event(event: GraphAuditEvent) -> None:
    print(format_audit_event(event), flush=True)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEYS.search(str(key)):
                sanitized[key] = "<redacted>"
            else:
                sanitized[key] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        if _JWT.search(value):
            return _JWT.sub("<redacted-jwt>", value)
        if value.lower().startswith("bearer "):
            return "Bearer <redacted>"
    return value
