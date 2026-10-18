# TraceBack POC

A lightweight investigation tool for reconstructing the original human identity behind an agent-driven action using Microsoft Sentinel / Log Analytics telemetry.

## What it does

The app helps reconstruct a chain similar to:

Bob → M365 Copilot Agent → Azure AI Foundry Agent → MCP Server (Function App / Container App) → Microsoft Graph → user1 marked compromised

It is a separate investigation workflow and does not instrument or modify the Copilot or Foundry agents being investigated.

## Scenario support

- Delegated permissions: downstream actions retain the original delegated user identity.
- Application permissions: Graph activity is performed by an app identity, so the tool correlates backwards through Sentinel telemetry to estimate the original human requester.

## Sentinel source types used

The POC primarily uses telemetry already available in Log Analytics / Sentinel, including tables such as:

- SigninLogs / AadNonInteractiveUserSignInLogs
- BehaviorAnalytics
- CloudAppEvents
- AzureActivity
- AppRequests
- MicrosoftGraphActivityLogs
- Defender / security incident data, when present

## Running locally

1. Install requirements:
   ```bash
   pip install -r requirements.txt
   ```

2. Configure the target workspace as described below, and authenticate Azure CLI
   as an identity with read access to that workspace. Authentication and any
   required CLI extension installation are operator setup, not performed by the app.

3. Start the app:
   ```bash
   python app.py
   ```

4. Open the page at http://localhost:5000

The server remains loopback-only. Targeting another tenant or subscription does
not enable remote browser access or change the Azure CLI default subscription.

## Configuration

There are no tenant, subscription, resource group, workspace name or workspace ID
defaults. Set process environment variables before starting the app; restart it
after changing them. The app does not load `.env` files and never guesses a
workspace from the current Azure CLI subscription.

Choose either a full resource ID (PowerShell):

```powershell
$env:LOG_ANALYTICS_WORKSPACE_RESOURCE_ID = "/subscriptions/<subscription-guid>/resourceGroups/<resource-group>/providers/Microsoft.OperationalInsights/workspaces/<workspace-name>"
```

Or all three components:

```powershell
$env:AZURE_SUBSCRIPTION_ID = "<subscription-guid>"
$env:LOG_ANALYTICS_RESOURCE_GROUP = "<resource-group>"
$env:LOG_ANALYTICS_WORKSPACE_NAME = "<workspace-name>"
```

These are independent optional settings for either form:

```powershell
$env:LOG_ANALYTICS_WORKSPACE_ID = "<customer-guid>"
$env:AZURE_TENANT_ID = "<expected-tenant-guid>"
```

Replace placeholders with your deployment's values. On shells that use `export`,
set the same variable names there instead. Unset unused optional variables;
setting an empty or whitespace-only value is an error.

| Setting | Behavior |
| --- | --- |
| `LOG_ANALYTICS_WORKSPACE_RESOURCE_ID` | Full resource path shown above, not a management URL, customer GUID or path with a trailing slash, query string or child resource. |
| `AZURE_SUBSCRIPTION_ID` | GUID required with the component form; optional with a full resource ID. Every CLI operation is explicitly scoped to the subscription in the validated resource ID. |
| `LOG_ANALYTICS_RESOURCE_GROUP` | Required with the component form. Supported names contain 1-90 ASCII letters, digits, `_`, `-`, `.`, `(` or `)`, and cannot end in a period. |
| `LOG_ANALYTICS_WORKSPACE_NAME` | Required with the component form. Contains 4-63 ASCII letters, digits or hyphens, starting and ending with a letter or digit. |
| `LOG_ANALYTICS_WORKSPACE_ID` | Optional customer/workspace GUID, always honored when supplied, including for other tenants and subscriptions. It does not replace the required resource identity. If omitted, a read-only workspace lookup resolves `customerId` before querying. |
| `AZURE_TENANT_ID` | Optional expected tenant GUID. Before every investigation, a read-only `az account show --subscription ... --query tenantId` checks the current CLI account for the configured subscription. A mismatch blocks workspace lookup and all telemetry queries. When omitted, no account lookup or tenant restriction is added. |

The Foundry and MCP telemetry table names default to the table names in the
test-rig design, but can be overridden per deployment. Override values must be
simple Log Analytics table identifiers; arbitrary KQL is rejected before any
query runs.

The affected target is matched only against resource-oriented fields such as
Graph request URIs and dependency names/targets. It is never treated as an
actor, application or request identifier. Valid rows that do not qualify as
evidence may contribute bounded correlation keys for later queries, but those
discovery hints are not returned as evidence and cannot support attribution.
Structured MCP application console events are treated this way explicitly:
they may help discover bounded identifiers, but only native host telemetry can
represent the MCP-service hop.

| Optional table setting | Default source |
| --- | --- |
| `TRACKBACK_TABLE_APP_REQUESTS` | `AppRequests` |
| `TRACKBACK_TABLE_APP_DEPENDENCIES` | `AppDependencies` |
| `TRACKBACK_TABLE_APP_TRACES` | `AppTraces` |
| `TRACKBACK_TABLE_APP_EVENTS` | `AppEvents` |
| `TRACKBACK_TABLE_APP_EXCEPTIONS` | `AppExceptions` |
| `TRACKBACK_TABLE_AZURE_DIAGNOSTICS` | `AzureDiagnostics` |
| `TRACKBACK_TABLE_CONTAINER_APP_CONSOLE_LOGS` | `ContainerAppConsoleLogs` (use `ContainerAppConsoleLogs_CL` for the legacy `log-analytics` destination) |
| `TRACKBACK_TABLE_CONTAINER_APP_HTTP_LOGS` | `ContainerAppHTTPLogs` |

When a full resource ID and any individual components are both supplied, every
supplied component must match its entire corresponding segment; nothing silently
overrides another setting. Comparison is case-insensitive for Azure resource
identities and GUIDs. Partial redundant components are allowed only when they
match. Values are not trimmed, and malformed redundant settings are still errors.
An explicitly supplied customer GUID is used as-is rather than looked up or
cross-checked against the resource ID; the operator must supply the GUID belonging
to that workspace.

Missing, malformed or conflicting configuration fails closed: `/api/health`
reports `ready: false`, and investigation requests return HTTP 503 with
`error: "invalid_workspace_configuration"`, a fixed reason code and a sanitized
message identifying the settings to fix. Tenant mismatches use the same error
contract with reason `tenant_mismatch`. No configured identifiers or account
details appear in health, configuration errors or application logs.

Health is a local configuration/executable check, not an authentication or
permission probe; it never runs Azure CLI. Tenant validation and customer ID
resolution happen only on investigation requests. Metadata lookup failures stop
all telemetry queries; individual telemetry failures remain visible while other
sources are attempted. Failures retain sanitized reason codes such as
`access_denied`, `timeout` and `invalid_response`. All CLI calls use the injectable, shell-free
runner, a maximum 20-second call timeout, a shared 90-second investigation budget
and a 2 MiB limit per output stream. The app does not log in, change account
context, install extensions or modify cloud resources.

## Using TraceBack in your own tenant

TraceBack itself has no tenant-specific code. It needs:

1. **A Log Analytics / Sentinel workspace** receiving these Microsoft logs:
   - Entra ID diagnostic settings: `SignInLogs`, `NonInteractiveUserSignInLogs`,
     `ServicePrincipalSignInLogs`, `ManagedIdentitySignInLogs`, `MicrosoftGraphActivityLogs`
   - Microsoft 365 Copilot audit (`CopilotActivity`) via the Sentinel data connector
   - Optional, for the app-only requester lead: Application Insights connected to
     each Foundry project, workspace-based on the same workspace
2. **Reader access** to that workspace for the identity used with `az login`.
3. The `LOG_ANALYTICS_*` environment variables from [Configuration](#configuration).

Run **Telemetry coverage** in the UI (or `GET /api/coverage`) first; it reports
which of these sources are missing.

### Optional: deploy the test rig

`testrig/` builds a disposable delegated (OBO) and app-only MCP server pair that
reads one user from Microsoft Graph, so you can generate known activity:

```powershell
$env:TB_TENANT_ID = "<tenant-id>"
$env:TB_SUBSCRIPTION_ID = "<subscription-id>"
$env:TB_RESOURCE_GROUP = "tb-<new-resource-group>"   # teardown only deletes tb-* groups
$env:TB_LAW_RESOURCE_GROUP = "<sentinel-resource-group>"
$env:TB_LAW_NAME = "<sentinel-workspace-name>"
$env:TB_TEST_TARGET_USER = "user1@contoso.com"
./testrig/deploy.ps1
```

Then, manually:
1. Grant admin consent for the delegated app and the managed identity's Graph permission.
2. In Foundry, create two agents, each with an MCP tool pointing at the
   `https://<container-app>/mcp` URLs printed by `deploy.ps1`.
3. Connect the Foundry project to the Application Insights resource the script created.
4. Publish both agents to Microsoft 365 Copilot.
5. As a test user (for example `user2`), ask each agent to look up `user1`, wait
   15-20 minutes for ingestion, then investigate `user1` in TraceBack.

`testrig/teardown.ps1` removes the rig. See `docs/TEST-RIG-ARCHITECTURE.md` and
`docs/TEST-RIG-IDENTITY-MODEL.md` for details and `testrig/NEXT-STEPS.md` for the
validation history.

## Last-hop verdict

Full human attribution (`human_attribution = supported`) requires all seven
hops to link exactly, which today's Copilot → Foundry → MCP hops cannot do
natively. Each response therefore also carries `last_hop`, which answers the
narrower question "whose token performed the API action on the target?":

| `verdict` | Meaning |
| --- | --- |
| `user_identified` | Delegated: the Graph row names one user. |
| `workload_identified` | App-only: the Graph row names only the workload; `human` is `not_in_api_telemetry`. |
| `multiple_actors` | Different identities acted on the target; review each action. |
| `unresolved` | The identity binding is missing or contradicts the sign-in log. |
| `no_evidence` | No API action in the selected permission mode. |

`strength` is `token_exact` when every action's Graph `SignInActivityId`
equals a successful sign-in `UniqueTokenIdentifier` for the same identity, and
`api_log_only` otherwise. Identities are opaque `corr-…` references; use
[docs/ANALYST-PIVOT.md](docs/ANALYST-PIVOT.md) in Sentinel to see who they are.

Queries rank each row as `MatchRank` 0 (per-event ID such as a token
`UniqueTokenIdentifier`/`SignInActivityId` or request ID), 1 (target), 2 (shared
session/trace/correlation ID) or 3 (broad user/app ID only). Rank 0–1 rows seed
later queries. Rank 2 rows seed them only when the shared session has at most 10
rows, so one busy user cannot flood the key set and hide the token-level row.
Overflow is reported by rank:
- rank 0–1 overflow: `row_limit`, and the source is marked truncated
- rank 2 overflow: `context_row_limit`, and the source is not marked truncated
- rank 3 overflow: not reported

Live investigations use Microsoft platform logs only. Logs an application writes
about itself (for example an MCP server's console output) are never queried, so
TraceBack works the same for any agent and needs no custom logging.

### Requester lead (app-only gap)

`last_hop.requester_lead` names the Copilot user(s) who prompted the agent
right before the tool call that made each Graph request. It uses only Microsoft
platform logs and **requires Application Insights connected to the Foundry
project** (an out-of-box project setting); without it, `status` is
`application_insights_unavailable`.

| Link | Join |
| --- | --- |
| Graph action → `AppDependencies` `execute_tool` span | same target, Graph time inside the span (±5 s) |
| span `microsoft.a365.agent.blueprint.id` → `AADServicePrincipalSignInLogs` `Agent.parentAppId` | exact ID |
| that sign-in's `ServicePrincipalId` (agent identity) → `CopilotActivity` `AgentId` suffix | exact ID |
| `CopilotInteraction` to that agent 0–15 min before the span → `ActorUserId` | agent + time |

Two links are target/time matches, so `proof` is always `false` and the lead
never changes `verdict` or `human_attribution`. `status` is `sole_candidate`,
`partial_candidate`, `multiple_candidates`, `no_tool_span`,
`no_agent_identity`, `no_agent_prompts`, `application_insights_unavailable` or
`query_failed`. In delegated mode, `agrees_with_token` checks the lead against
the token-proven user, which validates the method.

A query that times out is retried once if the investigation budget allows.

## Telemetry coverage self-check

An empty investigation only means "no evidence" when the native sources are
actually being ingested. `GET /api/coverage` (or `python -m trackback.coverage`)
reports, with fixed reason codes and no rows or KQL text:

- `daily_cap`: workspace daily cap and ingestion status;
- `workspace_transforms`: evidence tables whose workspace DCR transform filters rows;
- `entra_diagnostic_settings`: Entra categories not exported to the workspace;
- `table_ingestion`: core (Graph, sign-in) and supporting tables with no rows in 7 days;
- `graph_read_coverage`: whether Graph `GET /users` reads reach the workspace at all.

Run it before trusting a `no_evidence` result. It needs read access to the
workspace, its default DCR and `microsoft.aadiam/diagnosticSettings`; checks it
cannot read are reported as `unknown`, never `ok`.

Target search also matches the percent-encoded form of a UPN (Graph and MCP
URIs carry `user%40domain`) and, when Sentinel UEBA `IdentityInfo` is present,
the resolved object ID.

## Design notes

This is intentionally a small first-pass PoC:

- It queries live workspace telemetry in a simple way.
- It does not add custom telemetry or correlation IDs.
- It clearly marks where attribution becomes uncertain or impossible.
- It is structured so the correlation logic can be expanded into a richer triage model later.

## Example investigation flow

The tool accepts a target or identity string (such as `user1`, a user principal name, an app name, or a target resource) and performs a chained correlation search:

Original user → Copilot / Agent → Foundry agent → MCP tool → workload identity → API action → target

For each hop, the UI renders:

- value
- confidence level
- evidence table(s)
- timestamps
- correlation IDs
- uncertainty notes

## Limitations

This is a POC. It is intentionally conservative:

- If the relevant telemetry does not exist in the active time window, attribution is marked uncertain.
- If the app identity is the only visible actor, the original human can be impossible to identify without Entra sign-in or delegated token telemetry.
- The MCP hop is usually inferred from service telemetry, since the underlying action path may not carry a unique end-to-end correlation ID across all hop boundaries.
