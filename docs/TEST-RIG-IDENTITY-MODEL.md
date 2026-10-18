# Test Rig Identity Model

**Date:** 2026-09-19  
**Scope:** identity, permissions, consent, and attribution for the isolated TraceBack test rig.

This document supersedes the earlier `docs/GRAPH-EVIDENCE-SOURCES.md` no-go conclusion about Microsoft Graph activity-log availability. The target validation workspace already has live `MicrosoftGraphActivityLogs`; the old one-hour capped ingestion test is no longer part of this design.

## Named variables only

Do not hard-code tenant IDs, client IDs, subscriptions, resource groups, workspace IDs, or scope URIs in code, docs, prompts, or deployment files. Use these names:

| Variable | Meaning |
|---|---|
| `${TENANT_ID}` | Microsoft Entra tenant ID. |
| `${SUBSCRIPTION_ID}` | Azure subscription for the isolated rig. |
| `${RESOURCE_GROUP_NAME}` | Resource group for the isolated rig. |
| `${LOG_ANALYTICS_WORKSPACE_ID}` | Workspace customer/workspace ID used for TraceBack queries. |
| `${LOG_ANALYTICS_WORKSPACE_RESOURCE_ID}` | Full Azure resource ID of that workspace. |
| `${COPILOT_AGENT_ID}` | Microsoft 365 Copilot agent identifier. |
| `${COPILOT_AGENT_CLIENT_ID}` | Client/application ID used by the Copilot agent or its connector. |
| `${FOUNDRY_PROJECT_NAME}` | Azure AI Foundry project name. |
| `${FOUNDRY_AGENT_ID}` | Foundry agent identifier. |
| `${FOUNDRY_AGENT_API_CLIENT_ID}` | App registration client ID for the protected Foundry-facing API, if the Foundry layer accepts Entra user tokens directly. |
| `${FOUNDRY_AGENT_API_APP_ID_URI}` | Exposed API URI, normally `api://${FOUNDRY_AGENT_API_CLIENT_ID}`. |
| `${MCP_SERVER_API_CLIENT_ID}` | App registration client ID for the protected MCP server API. |
| `${MCP_SERVER_API_APP_ID_URI}` | Exposed API URI, normally `api://${MCP_SERVER_API_CLIENT_ID}`. |
| `${MCP_SERVER_MANAGED_IDENTITY_CLIENT_ID}` | User-assigned managed identity client ID for app-only mode. |
| `${MCP_SERVER_MANAGED_IDENTITY_PRINCIPAL_ID}` | Service principal/object ID of that managed identity. |
| `${MCP_SERVER_APP_CLIENT_ID}` | Fallback app registration client ID for app-only client credentials if managed identity cannot be used. |
| `${MCP_SERVER_APP_SERVICE_PRINCIPAL_ID}` | Service principal/object ID for `${MCP_SERVER_APP_CLIENT_ID}`. |
| `${TEST_TARGET_USER_ID}` | Disposable test user object ID or another approved read-only Graph target. |
| `${DELEGATED_GRAPH_SCOPES}` | Delegated Graph scopes approved for Scenario A. |
| `${APPLICATION_GRAPH_ROLES}` | Application Graph roles approved for Scenario B. |
| `${TRACEBACK_READER_CLIENT_ID}` | Separate read-only investigation identity, if used. It is not in the test execution path. |

## Permission baseline

The test rig must generate read-only Microsoft Graph calls only.

Recommended single Graph action for both scenarios:

```text
GET https://graph.microsoft.com/v1.0/users/${TEST_TARGET_USER_ID}?$select=id,displayName,userPrincipalName
```

Recommended permissions:

| Purpose | Scenario A delegated | Scenario B application | Notes |
|---|---|---|---|
| Read signed-in user's profile during login, if needed | `User.Read` | Not applicable | Safe for sign-in bootstrap only. |
| Read the disposable target user's basic profile | `User.ReadBasic.All` | `User.ReadBasic.All` | Least-privilege read-only permission for basic user profile lookup across users. |
| Read audit/sign-in data from Graph | Do **not** grant to MCP by default | Do **not** grant to MCP by default | `AuditLog.Read.All` belongs on the separate TraceBack reader identity only if TraceBack must call Graph audit APIs directly. It is not needed to make the test Graph call. |

No `*.ReadWrite.*`, `Directory.ReadWrite.All`, `Directory.AccessAsUser.All`, `Application.ReadWrite.*`, `Group.ReadWrite.*`, Mail write, Files write, or mutation permission is allowed in this rig.

## Scenario A: delegated path

### Goal

Force the final Microsoft Graph request to be made with a delegated token so `MicrosoftGraphActivityLogs.UserId` is populated with the signed-in user's Microsoft Entra object ID.

### Required app registrations

Use separate app registrations so every hop has a clear audience and consent boundary:

1. **Copilot/client app**
   - Client ID: `${COPILOT_AGENT_CLIENT_ID}`
   - Represents the Copilot agent or connector that starts the user-driven flow.
   - Requests a token for `${FOUNDRY_AGENT_API_APP_ID_URI}/access_as_user` or, if Copilot calls MCP directly, `${MCP_SERVER_API_APP_ID_URI}/access_as_user`.

2. **Foundry-facing protected API app**
   - Client ID: `${FOUNDRY_AGENT_API_CLIENT_ID}`
   - Application ID URI: `${FOUNDRY_AGENT_API_APP_ID_URI}`
   - Exposes delegated scope: `access_as_user`
   - Manifest/consent setup:
     - `knownClientApplications` includes `${COPILOT_AGENT_CLIENT_ID}` where combined consent is required.
     - Pre-authorize `${COPILOT_AGENT_CLIENT_ID}` for `access_as_user` when tenant policy permits.
   - Required API permissions:
     - Delegated permission to MCP API: `${MCP_SERVER_API_APP_ID_URI}/access_as_user`

3. **MCP server protected API app**
   - Client ID: `${MCP_SERVER_API_CLIENT_ID}`
   - Application ID URI: `${MCP_SERVER_API_APP_ID_URI}`
   - Exposes delegated scope: `access_as_user`
   - Manifest/consent setup:
     - `knownClientApplications` includes `${FOUNDRY_AGENT_API_CLIENT_ID}`.
     - Pre-authorize `${FOUNDRY_AGENT_API_CLIENT_ID}` for `access_as_user` when tenant policy permits.
   - Required Microsoft Graph delegated permissions:
     - `User.ReadBasic.All`
     - `User.Read` only if the MCP server or upstream API must call `/me` for bootstrap.

### Token path

```text
Signed-in human
  -> Copilot/client obtains token for ${FOUNDRY_AGENT_API_APP_ID_URI}/access_as_user
  -> Foundry-facing API validates token audience, issuer, tenant, expiry, and user claims
  -> Foundry-facing API uses OAuth 2.0 On-Behalf-Of (OBO) to get token for ${MCP_SERVER_API_APP_ID_URI}/access_as_user
  -> MCP server validates token audience, issuer, tenant, expiry, and user claims
  -> MCP server uses OBO to get Microsoft Graph delegated token with ${DELEGATED_GRAPH_SCOPES}
  -> MCP server calls Microsoft Graph read-only endpoint
  -> MicrosoftGraphActivityLogs records delegated request with UserId populated
```

### OBO requirements

- Every middle tier must receive a user delegated access token whose `aud` matches that middle tier.
- The middle tier must be a confidential client or otherwise able to authenticate securely to Microsoft Entra ID.
- OBO is delegated-only. Do not mix application roles into the delegated chain.
- Request `.default` only for combined consent flows. Do not combine `.default` with named delegated scopes in the same authorization request.
- Validate `iss`, `tid`, `aud`, `exp`, `nbf`, and the expected delegated scope at every API boundary.
- Reject tokens from any tenant other than `${TENANT_ID}` unless the rig is explicitly made multi-tenant.
- Use conditional access and admin consent normally; do not bypass policy for the test.

### Claims that survive to Microsoft Graph

The final Graph access token is the only token Microsoft Graph sees. Expected Graph-visible identity:

| Claim/evidence | Expected result at Graph | Effect in `MicrosoftGraphActivityLogs` |
|---|---|---|
| User object ID from final delegated token (`oid`/subject mapped by Entra) | Survives | `UserId` is populated with the human user's object ID. |
| Tenant ID (`tid`) | Survives | `AadTenantId` identifies `${TENANT_ID}`. |
| Delegated scopes (`scp`) | Survives | `Scopes` contains `User.ReadBasic.All` and optionally `User.Read`. |
| Application identity of final token-acquiring client | Survives | `AppId`/`ServicePrincipalId` identify the MCP server app registration/service principal. |
| Upstream Copilot agent ID | Does not normally survive into the Graph token | Must be correlated through `CopilotActivity`/Foundry/MCP telemetry, not Graph claims. |
| Foundry agent ID | Does not normally survive into the Graph token | Must be correlated through Foundry telemetry, not Graph claims. |
| MCP request/trace ID | Does not normally survive unless a platform-generated dependency/request identifier is emitted elsewhere | Do not rely on custom headers for attribution. |

## Scenario B: application path

### Goal

Force the final Microsoft Graph request to be made with an application token so `MicrosoftGraphActivityLogs.UserId` is empty and `Roles` contains the application permission.

### Recommended identity: user-assigned managed identity

Use a user-assigned managed identity for the MCP server app-only path when the MCP host is Azure Functions, Container Apps, App Service, or another Azure host that supports managed identity.

Why this is the better default:

- No client secret or certificate to store, rotate, leak, or accidentally commit.
- Sign-ins land in `AADManagedIdentitySignInLogs`, which is specifically scoped to managed identities.
- The managed identity can be tied back to the Azure resource assignment by its principal/client ID.
- The identity can be removed from the host during teardown without touching any human credential.

Fallback only if managed identity is impossible:

- Use `${MCP_SERVER_APP_CLIENT_ID}` with certificate-based client credentials or workload identity federation.
- Avoid client secrets. If a secret is unavoidable, store it only in Key Vault and reference it from the host configuration. Never store it in source control, `.env*`, deployment output, or documentation.
- App-only sign-ins for this fallback land in `AADServicePrincipalSignInLogs`.

### Required Graph application permissions

For the recommended read-only user lookup:

```text
${APPLICATION_GRAPH_ROLES}=User.ReadBasic.All
```

Do not grant `AuditLog.Read.All` to the app-only MCP identity unless the MCP server's actual Graph call is intentionally `GET /auditLogs/...`. That is not recommended for the primary rig because it broadens access and mixes the test actor with the investigation reader.

### Token path

Managed identity path:

```text
Human starts Copilot interaction
  -> Copilot/Foundry invokes MCP server
  -> MCP server uses DefaultAzureCredential scoped to ${MCP_SERVER_MANAGED_IDENTITY_CLIENT_ID}
  -> Azure Instance Metadata/managed identity endpoint issues an app-only token for Microsoft Graph
  -> MCP server calls Microsoft Graph read-only endpoint
  -> MicrosoftGraphActivityLogs records app-only request with UserId empty
```

Client-credentials fallback:

```text
Human starts Copilot interaction
  -> Copilot/Foundry invokes MCP server
  -> MCP server authenticates as ${MCP_SERVER_APP_CLIENT_ID} using federated/certificate credential
  -> Microsoft Entra ID issues an app-only token for Microsoft Graph
  -> MCP server calls Microsoft Graph read-only endpoint
  -> MicrosoftGraphActivityLogs records app-only request with UserId empty
```

### What Microsoft Graph will and will not see

| Information | Managed identity path | Client-credentials app path |
|---|---|---|
| UserId in `MicrosoftGraphActivityLogs` | Empty | Empty |
| `Roles` in `MicrosoftGraphActivityLogs` | Contains `User.ReadBasic.All` | Contains `User.ReadBasic.All` |
| `Scopes` in `MicrosoftGraphActivityLogs` | Empty | Empty |
| `AppId` | Managed identity client/application ID | App registration client ID |
| `ServicePrincipalId` | Managed identity service principal/object ID | App registration service principal/object ID |
| Token/sign-in join | `SignInActivityId` or `UniqueTokenId` to `AADManagedIdentitySignInLogs.UniqueTokenIdentifier` | `SignInActivityId` or `UniqueTokenId` to `AADServicePrincipalSignInLogs.UniqueTokenIdentifier` |
| Original human requester | Not in the Graph token | Not in the Graph token |
| Copilot/Foundry/MCP request IDs | Not in the Graph token unless some platform telemetry independently records matching identifiers | Same |

## Scenario B attribution analysis

Application permissions intentionally erase the human from the final Graph token. The human can only be recovered by walking backward through platform telemetry. Exact identifier joins are acceptable evidence. Time-window-only joins are candidates, not proof.

### Backward-correlation paths

| Path | Tables and fields | Join type | Confidence | False-attribution risk |
|---|---|---:|---|---|
| B1. Graph request to workload token sign-in | `MicrosoftGraphActivityLogs.SignInActivityId` or `UniqueTokenId` -> `AADManagedIdentitySignInLogs.UniqueTokenIdentifier` for managed identity; or -> `AADServicePrincipalSignInLogs.UniqueTokenIdentifier` for client credentials. Cross-check `AppId` and `ServicePrincipalId`. | Exact identifier | High for workload identity; none for human | Low for workload identity. Does not identify the human. |
| B2. Managed identity sign-in to MCP Azure resource | `AADManagedIdentitySignInLogs.ServicePrincipalId` / `AppId` / `SourceAppClientId` -> Azure resource identity assignment where `identity.principalId == ${MCP_SERVER_MANAGED_IDENTITY_PRINCIPAL_ID}` or `identity.clientId == ${MCP_SERVER_MANAGED_IDENTITY_CLIENT_ID}`; corroborate with host telemetry `AppRequests.CloudRoleName` / `_ResourceId` when present. | Exact config lookup plus telemetry corroboration | High for MCP host identity | Low if a single user-assigned identity is assigned to only the rig host. Medium if reused across resources. |
| B3. Service-principal app-only sign-in to MCP app | `AADServicePrincipalSignInLogs.ServicePrincipalId` / `AppId` -> `${MCP_SERVER_APP_SERVICE_PRINCIPAL_ID}` / `${MCP_SERVER_APP_CLIENT_ID}`; corroborate with MCP host configuration that names the same client ID. | Exact identifier/config lookup | High for app identity | Low for app identity. Medium if the same app registration is reused by multiple hosts. |
| B4. MCP request to Graph request | `AppDependencies` Graph dependency row (`OperationId`, `ParentId`, `Id`, target/name/result code/time) -> `AppRequests` MCP request row (`OperationId`, `Id`). Then candidate-match to `MicrosoftGraphActivityLogs` by `TimeGenerated`, `AppId`, `ServicePrincipalId`, `RequestMethod`, `RequestUri`, and `ResponseStatusCode`; exact only if a platform-generated `ClientRequestId` appears in both sources. | Exact inside App Insights; usually heuristic to Graph | Medium; High only with shared platform request ID | Medium. Two Graph calls by the same MCP identity to the same URI in the same short window can be confused. |
| B5. Foundry invocation to MCP request | `AppRequests`/`AppDependencies`/`AppTraces` for Foundry (`OperationId`, `Id`, `ParentId`, W3C trace/span fields, `Properties["gen_ai.agent.id"]`) -> MCP `AppRequests`/`AppDependencies` (`OperationId`, `ParentId`, `Id`) when trace context propagates. | Exact if W3C trace is preserved; otherwise heuristic by URL/time/agent ID | High with trace propagation; Medium without it | Low with exact trace IDs. Medium if relying on URL plus time. |
| B6. Copilot interaction to Foundry invocation | `CopilotActivity.ActorUserId`, `AgentId`/`AppIdentity`, `ConversationId`, `ThreadId`, message/task identifiers -> Foundry telemetry properties if the platform emits matching conversation/thread/task/agent identifiers. | Exact only when a shared platform identifier exists; otherwise heuristic by user, agent, and time | High with shared identifier; Low-to-Medium without it | Low with exact conversation/thread/message ID. High if only one user's Copilot activity is near one Foundry invocation. |
| B7. Human sign-in corroboration | `SigninLogs.UserId` or `AADNonInteractiveUserSignInLogs.UserId` -> `CopilotActivity.ActorUserId`, then time proximity to the Copilot interaction. | Exact user ID plus time | Medium as corroboration only | Medium. It proves the user authenticated to a client, not that they caused the app-only Graph call. |
| B8. Azure control-plane owner/deployer context | `AzureActivity.Caller`, `OperationNameValue`, `_ResourceId`, `CorrelationId` for creating/updating the host, identity assignment, or app settings. | Exact for deployment action; not invocation | Low for requester attribution | High. Deployer/operator may not be the human who invoked the agent. Use only as context. |

### Practical confidence ladder

| Result | Required evidence |
|---|---|
| **High confidence human attribution** | B1 exact workload identity join **plus** B4 exact or strongly bounded Graph-to-MCP link **plus** B5 exact trace link **plus** B6 exact Copilot-to-Foundry identifier showing `${COPILOT_AGENT_ID}` and `ActorUserId`. |
| **Medium confidence human attribution** | B1 exact workload identity join plus exact H3/H4 App Insights trace, but B4 or B6 relies on a narrow time window. State that it is a candidate. |
| **Low confidence human attribution** | B1 exact workload identity only, with human inferred from Copilot/Foundry events by time. Do not call this proof. |
| **No human attribution** | Graph app-only row and workload sign-in exist, but no reliable Copilot/Foundry/MCP chain. Report the workload identity only. |

The main false-attribution case is two users driving the same Copilot agent or Foundry agent while the same MCP managed identity makes the same Graph read in the same window. In that case, the app-only Graph row cannot pick the human by itself.

## What the human must click or approve

These steps cannot be done headlessly by the test rig. The wording here is intentionally plain.

1. **Create or approve the delegated test app registrations**
   - Where: Microsoft Entra admin center -> App registrations.
   - What to click: create the Copilot/client app if needed, the Foundry-facing API app if used, and the MCP server API app.
   - What to fill in: the display names, redirect URI if the UI asks for one, and the exposed API scopes named `access_as_user`.
   - Time: about 10-20 minutes if you already know the names.

2. **Approve delegated permissions**
   - Where: Microsoft Entra admin center -> App registrations -> each app -> API permissions.
   - What to click: add the MCP API `access_as_user` permission to the Foundry app, add Microsoft Graph `User.ReadBasic.All` to the MCP app, then click "Grant admin consent" if the tenant asks for admin approval.
   - Time: about 5-10 minutes.

3. **Approve the app-only managed identity permission**
   - Where: Microsoft Entra admin center -> Enterprise applications, or the Azure portal identity page if it links you there.
   - What to click: find the managed identity for the MCP server, add Microsoft Graph `User.ReadBasic.All` application permission, and grant admin consent.
   - Time: about 5-10 minutes.

4. **If managed identity cannot be used, approve the fallback app-only app**
   - Where: Microsoft Entra admin center -> App registrations.
   - What to click: create the fallback MCP app registration, add Microsoft Graph `User.ReadBasic.All` application permission, and grant admin consent.
   - Time: about 10 minutes.
   - Important: do not create a client secret unless there is no other option. If a secret is unavoidable, put it in Key Vault only.

5. **Create or connect the Copilot agent**
   - Where: the Microsoft 365 Copilot agent builder or the place your tenant uses to create Copilot agents.
   - What to click: create the test agent, point it at the approved Foundry/MCP endpoint, and choose the sign-in option that uses the delegated app registration.
   - Time: about 15-30 minutes depending on the UI.

6. **Create or connect the Foundry agent**
   - Where: Azure AI Foundry portal -> the test Foundry project.
   - What to click: create/select the test agent, attach the MCP tool endpoint, and connect the approved Application Insights/monitoring setup documented in the architecture.
   - Time: about 15-30 minutes.

7. **Turn on the required platform diagnostics**
   - Where: Azure portal pages for the Foundry project, MCP host, and Microsoft Entra diagnostic settings.
   - What to click: send Foundry, MCP host, sign-in, audit, and Graph activity logs to `${LOG_ANALYTICS_WORKSPACE_ID}`.
   - Time: about 10-20 minutes if the workspace already exists.

8. **Run the two test prompts**
   - Where: the Copilot agent chat UI.
   - What to click/type: run one prompt that uses delegated mode and one prompt that uses app-only mode.
   - Time: about 5 minutes.

## Security review

### Blast radius

- The rig is isolated to `${SUBSCRIPTION_ID}`, `${RESOURCE_GROUP_NAME}`, `${FOUNDRY_PROJECT_NAME}`, the dedicated Copilot/Foundry agents, and the dedicated MCP host.
- The Graph target is a disposable read-only lookup: `${TEST_TARGET_USER_ID}`.
- The MCP execution identity must not be reused by production services.
- The user-assigned managed identity must be assigned only to the rig MCP host unless a security owner explicitly approves broader use.

### Why all Graph permissions are read-only

- The purpose is telemetry attribution, not changing directory state.
- `User.ReadBasic.All` is enough to produce a Microsoft Graph request with a clear delegated or application permission mode.
- `AuditLog.Read.All`, if needed by TraceBack, is for the separate reader identity and is still read-only.
- No write permission improves attribution. Write permissions only increase blast radius.

### Secret handling

- Prefer managed identity for Scenario B.
- Prefer OBO with confidential clients backed by platform-managed credentials or federated credentials for Scenario A middle tiers.
- If a secret is unavoidable:
  - Store it in Key Vault.
  - Reference it from the hosting platform.
  - Never put it in `.env*`, source code, docs, deployment logs, or chat output.
  - Rotate it immediately after the test if it was exposed to any human workflow.

### Teardown and revocation

1. Disable the Copilot test agent or remove its connection to the test endpoint.
2. Delete or disable the Foundry test agent.
3. Remove the MCP host's user-assigned managed identity assignment.
4. Remove Microsoft Graph app-role assignments from `${MCP_SERVER_MANAGED_IDENTITY_PRINCIPAL_ID}` and/or `${MCP_SERVER_APP_SERVICE_PRINCIPAL_ID}`.
5. Remove delegated API permissions and admin-consent grants from `${FOUNDRY_AGENT_API_CLIENT_ID}` and `${MCP_SERVER_API_CLIENT_ID}`.
6. Delete unused app registrations only after exports/evidence are complete.
7. Delete Key Vault secrets created only for the fallback path.
8. Confirm `AADManagedIdentitySignInLogs`, `AADServicePrincipalSignInLogs`, and `MicrosoftGraphActivityLogs` show no new test-rig activity after teardown.
9. Keep retained telemetry only under the approved workspace retention policy; do not copy raw logs into the repository.

## Cross-dependencies

- The architecture owns `docs/TEST-RIG-ARCHITECTURE.md`: resource layout, diagnostics routing, cost, and hosting choice.
- The MCP server owns `testrig/mcp-server/`: implementation of delegated OBO mode and app-only `DefaultAzureCredential` mode.
- Security requirement for the MCP server: expose auth mode by environment variable, but never use an environment variable for secrets. Managed identity or federated credentials are preferred.
- Security requirement for the architecture: keep all telemetry routed to `${LOG_ANALYTICS_WORKSPACE_ID}` and ensure the app-only path can be distinguished as managed identity versus service principal.

## References

- Microsoft identity platform OAuth 2.0 On-Behalf-Of flow: https://learn.microsoft.com/entra/identity-platform/v2-oauth2-on-behalf-of-flow
- Expose scopes in a protected web API: https://learn.microsoft.com/entra/identity-platform/scenario-protected-web-api-expose-scopes
- Microsoft Graph permissions overview: https://learn.microsoft.com/graph/permissions-overview
- Microsoft Graph permissions reference: https://learn.microsoft.com/graph/permissions-reference
- MicrosoftGraphActivityLogs table: https://learn.microsoft.com/azure/azure-monitor/reference/tables/microsoftgraphactivitylogs
- AADManagedIdentitySignInLogs table: https://learn.microsoft.com/azure/azure-monitor/reference/tables/aadmanagedidentitysigninlogs
- AADServicePrincipalSignInLogs table: https://learn.microsoft.com/azure/azure-monitor/reference/tables/aadserviceprincipalsigninlogs
