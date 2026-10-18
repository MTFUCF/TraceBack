# TraceBack test-rig MCP server

This isolated server provides the H4 test-rig hop for TraceBack. It exposes a small MCP-style JSON-RPC surface over HTTP and performs only Microsoft Graph `GET` requests.

## Why FastAPI

This implementation uses FastAPI rather than the official Python MCP SDK because the test rig needs explicit control over inbound bearer-token handling, OBO exchange, HTTP headers, and single-line stdout evidence in Container Apps. The `/mcp` endpoint supports the MCP `tools/list` and `tools/call` methods, and `/tools/{tool_name}` is also available for simple local probing.

## Tools

All tools call Microsoft Graph with `GET` only:

- `get_user_profile`
  - Delegated mode with no argument: `GET /me`
  - With `user_id_or_upn`: `GET /users/{user_id_or_upn}`
- `get_user_by_upn`
  - `GET /users/{upn}`
- `list_recent_signins`
  - `GET /auditLogs/signIns?$top={top}`
  - Optional `user_principal_name` filter.

No tool performs writes, patches, deletes, directory mutations, or consent changes.

## Environment variables

The server reads configuration only from process environment variables. It does not read `.env` files.

| Variable | Required | Description |
| --- | --- | --- |
| `TB_AUTH_MODE` | Yes | `delegated` or `application`. |
| `TB_GRAPH_SCOPES` | Yes | Comma-separated scopes passed to MSAL or Azure Identity, for example the scopes/app roles security review approves. No default is assumed. |
| `TB_TENANT_ID` | Delegated; application with `TB_CLIENT_SECRET` | Tenant for MSAL OBO or client-credentials auth. |
| `TB_CLIENT_ID` | Delegated; application with `TB_CLIENT_SECRET` | App registration client ID. |
| `TB_CLIENT_SECRET` | Optional | Secret for MSAL OBO or app-only client credentials. Never logged. If omitted in delegated mode, the server uses the incoming bearer token directly for Graph. If omitted in application mode, the server uses `DefaultAzureCredential`. |
| `TB_MANAGED_IDENTITY_CLIENT_ID` | Optional | User-assigned managed identity client ID for `DefaultAzureCredential`. Omit for system-assigned managed identity or local developer credentials. |
| `TB_GRAPH_BASE_URL` | Optional | HTTPS Graph base URL. Defaults to `https://graph.microsoft.com/v1.0`. Override for sovereign clouds if required. |
| `TB_PLATFORM_ID_HEADERS` | Optional | Comma-separated inbound header names whose existing platform-generated values should be copied into logs, for example Foundry thread/run headers once confirmed the exact names. The server never generates these IDs. |

Configuration fails closed when required values are missing, blank, padded, or malformed.

## Auth modes

### Delegated

Set:

```powershell
$env:TB_AUTH_MODE = "delegated"
$env:TB_GRAPH_SCOPES = "<security-approved delegated Graph scopes>"
$env:TB_TENANT_ID = "<tenant-guid-or-name>"
$env:TB_CLIENT_ID = "<app-client-id>"
$env:TB_CLIENT_SECRET = "<app-client-secret, only if using OBO>"
```

Each request must include `Authorization: Bearer <incoming-user-token>`. If `TB_CLIENT_SECRET` is set, the server uses MSAL `acquire_token_on_behalf_of` to exchange that incoming user token for a Microsoft Graph access token. If `TB_CLIENT_SECRET` is not set, the incoming token is sent directly to Microsoft Graph, so the caller must provide a Graph-scoped delegated token.

### Application

Client credentials:

```powershell
$env:TB_AUTH_MODE = "application"
$env:TB_GRAPH_SCOPES = "<security-approved application scope, often Graph .default>"
$env:TB_TENANT_ID = "<tenant-guid-or-name>"
$env:TB_CLIENT_ID = "<app-client-id>"
$env:TB_CLIENT_SECRET = "<app-client-secret>"
```

Managed identity or local `DefaultAzureCredential` chain:

```powershell
$env:TB_AUTH_MODE = "application"
$env:TB_GRAPH_SCOPES = "<security-approved application scope>"
# Optional for user-assigned managed identity:
$env:TB_MANAGED_IDENTITY_CLIENT_ID = "<managed-identity-client-id>"
```

## Structured stdout JSON schema

One single-line JSON object is emitted after every Microsoft Graph call:

```json
{
  "schema_version": 3,
  "timestamp_utc": "ISO-8601 UTC timestamp ending in Z",
  "event": "graph_request",
  "tool_name": "get_user_profile | get_user_by_upn | list_recent_signins",
  "auth_mode": "delegated | application",
  "graph_request_uri": "Full Microsoft Graph URI called",
  "http_status": 200,
  "graph_client_request_id": "GUID the server generated and sent as client-request-id (Graph logs it as ClientRequestId)",
  "graph_request_id": "Graph response request-id header, or null",
  "token_claims": {
    "oid": "optional inbound token object id",
    "sub": "optional inbound token subject",
    "appid": "optional inbound token app id",
    "azp": "optional authorized party",
    "tid": "optional tenant id",
    "scp": "optional delegated scope names",
    "roles": ["optional app role names"],
    "uti": "inbound token unique id (joins SigninLogs/AADNonInteractiveUserSignInLogs UniqueTokenIdentifier)",
    "idtyp": "optional token type (user | app)",
    "aud": "optional audience"
  },
  "platform_ids": {
    "traceparent": "W3C trace context from the caller, when sent",
    "configured-header-name": "value already present on inbound request"
  },
  "outbound_token_source": "obo | default_azure_credential | client_credentials | incoming_delegated_token",
  "outbound_token_claims": {
    "oid": "identity Graph sees (the user for OBO, the managed identity for app-only)",
    "appid": "client app Graph sees",
    "uti": "Graph token unique id (equals Graph SignInActivityId and the sign-in UniqueTokenIdentifier)",
    "idtyp": "user | app"
  },
  "inbound_header_names": ["lower-case header names only, never values"],
  "platform_baggage": {
    "baggage | x-ms-foundry-baggage | correlation-context | mcp_meta": {
      "key": "identifier-shaped value | <omitted:len=N> | <redacted>"
    }
  }
}
```

`platform_baggage` (schema v3) parses W3C baggage-format headers and the MCP `params._meta` object. A value is kept only if it is identifier-shaped: at most 200 characters from `[A-Za-z0-9._:@/+=-]`. Free text is reduced to its length, and values whose key looks secret (token, secret, password, credential, assertion, authorization, cookie, `*key`) are redacted. Purpose: to find a platform ID, such as a conversation or response ID, that binds an app-only call to the Copilot/Foundry request that caused it.

The server never logs access tokens, secrets, authorization headers, full token payloads, passwords, or credentials. Header values are logged only for the fixed correlation allowlist (`traceparent`, `x-ms-client-request-id`, `x-ms-correlation-request-id`, `x-ms-correlation-id`, `x-correlation-id`, `x-request-id`, `x-ms-request-id`, `request-id`) plus `TB_PLATFORM_ID_HEADERS`.

### Identity binding (schema v2)

Each line binds the MCP hop to native telemetry with exact identifiers, without timing:

| Audit field | Native join |
| --- | --- |
| `graph_client_request_id` | `MicrosoftGraphActivityLogs.ClientRequestId` |
| `outbound_token_claims.uti` | `MicrosoftGraphActivityLogs.SignInActivityId` and sign-in `UniqueTokenIdentifier` |
| `token_claims.uti` (delegated) | sign-in `UniqueTokenIdentifier` for the token the caller presented to MCP |
| `token_claims.oid` (delegated) | the human; app-only calls receive no user token, so this is empty |
| `platform_ids.traceparent` | Application Insights `OperationId` when the caller propagates W3C trace context |

These lines are self-reported by the MCP server. TraceBack uses them for discovery only, not as attribution proof.

## Run locally

Install dependencies:

```powershell
python -m pip install -r testrig\mcp-server\requirements.txt
```

Start the server:

```powershell
python -m uvicorn tb_mcp_server.server:app --app-dir testrig\mcp-server --host 127.0.0.1 --port 8000
```

List tools:

```powershell
curl -X POST http://127.0.0.1:8000/mcp -H "Content-Type: application/json" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{}}"
```

Call a tool:

```powershell
curl -X POST http://127.0.0.1:8000/mcp -H "Content-Type: application/json" -H "Authorization: Bearer <incoming-user-token-for-delegated-mode>" -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"get_user_profile\",\"arguments\":{}}}"
```

## Assumptions for architecture and security review

- Security review will provide the exact Graph scopes/app roles and admin-consent model for the three read-only tools.
- Architecture review will confirm the Container Apps ingress path and any Foundry platform-native thread/run header names to place in `TB_PLATFORM_ID_HEADERS`.
- If no platform-native Foundry request/run identifier is available on the inbound request, this server will not invent one.
