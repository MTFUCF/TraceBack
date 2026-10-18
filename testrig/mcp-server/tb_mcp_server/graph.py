from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode
import uuid

import httpx

from .audit import GraphAuditEvent, emit_audit_event
from .auth import TokenProvider
from .config import AuthMode, ServerConfig
from .token_claims import safe_token_claims


@dataclass(frozen=True)
class ToolContext:
    incoming_user_token: str | None = None
    platform_ids: dict[str, str] | None = None
    inbound_header_names: tuple[str, ...] = ()
    platform_baggage: dict[str, dict[str, str]] | None = None


class GraphToolService:
    def __init__(self, config: ServerConfig, token_provider: TokenProvider | None = None, http_client: httpx.Client | None = None):
        self.config = config
        self.token_provider = token_provider or TokenProvider(config)
        self.http_client = http_client or httpx.Client(timeout=20)

    def get_user_profile(self, context: ToolContext, user_id_or_upn: str | None = None) -> Any:
        if self.config.auth_mode is AuthMode.DELEGATED and user_id_or_upn is None:
            path = "/me"
        elif user_id_or_upn:
            path = f"/users/{quote(user_id_or_upn, safe='')}"
        else:
            raise ValueError("user_id_or_upn is required in application auth mode.")
        return self._graph_get("get_user_profile", path, context)

    def get_user_by_upn(self, context: ToolContext, upn: str) -> Any:
        if not upn:
            raise ValueError("upn is required.")
        return self._graph_get("get_user_by_upn", f"/users/{quote(upn, safe='')}", context)

    def list_recent_signins(self, context: ToolContext, top: int = 10, user_principal_name: str | None = None) -> Any:
        if top < 1 or top > 50:
            raise ValueError("top must be between 1 and 50.")
        query: dict[str, str | int] = {"$top": top}
        if user_principal_name:
            escaped = user_principal_name.replace("'", "''")
            query["$filter"] = f"userPrincipalName eq '{escaped}'"
        return self._graph_get("list_recent_signins", f"/auditLogs/signIns?{urlencode(query)}", context)

    def _graph_get(self, tool_name: str, path_and_query: str, context: ToolContext) -> Any:
        token_result = self.token_provider.get_graph_token(context.incoming_user_token)
        url = f"{self.config.graph_base_url}{path_and_query}"
        # A caller-chosen ID is persisted by Graph as ClientRequestId, binding this
        # audit line to the exact MicrosoftGraphActivityLogs row.
        client_request_id = str(uuid.uuid4())
        response = self.http_client.get(url, headers={
            "Authorization": f"Bearer {token_result.token}",
            "Accept": "application/json",
            "client-request-id": client_request_id,
            "return-client-request-id": "true",
        })
        emit_audit_event(GraphAuditEvent(
            tool_name=tool_name,
            auth_mode=str(self.config.auth_mode.value),
            graph_request_uri=url,
            http_status=response.status_code,
            graph_client_request_id=client_request_id,
            graph_request_id=response.headers.get("request-id"),
            token_claims=safe_token_claims(context.incoming_user_token),
            platform_ids=context.platform_ids or {},
            outbound_token_source=token_result.source,
            outbound_token_claims=safe_token_claims(token_result.token),
            inbound_header_names=context.inbound_header_names,
            platform_baggage=context.platform_baggage or {},
        ))
        response.raise_for_status()
        return response.json()
