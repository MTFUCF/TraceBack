# Test Rig Architecture for TraceBack Foundry + MCP Attribution

**Date:** 2026-09-19  
**Status:** Design only. No cloud resources have been created, modified, or deleted.

## 0. Architectural stance

Build a new, isolated rig with a distinct `tb-` prefix and route every required platform telemetry source into the existing analysis workspace variable `${TB_LAW_RESOURCE_ID}`. The rig exists to prove two different attribution outcomes:

- **Scenario A - delegated permissions:** the Graph request keeps the human user as a delegated subject, so `MicrosoftGraphActivityLogs.UserId` is populated and `Scopes` is nonempty.
- **Scenario B - application permissions:** Graph sees only the app or managed identity, so `MicrosoftGraphActivityLogs.UserId` is empty and TraceBack must correlate backward through Copilot, Foundry, MCP, and identity logs.

The rig must not rely on invented run IDs, custom correlation headers, prompt markers, or target-description markers. It may use native platform identifiers emitted by Copilot, Foundry, Application Insights/OpenTelemetry, Container Apps ingress, Entra sign-ins, and Microsoft Graph activity logs. Where no exact platform-generated key crosses a boundary, the result is `partial` or `unresolved`, not attributed.

## 1. Variable contract and portability

Use variables everywhere. Values below are current defaults for this tenant, not hard-coded design dependencies.

| Variable | Current/default value | Purpose | TraceBack env-var mapping |
|---|---|---|---|
| `TB_TENANT_ID` | `<tenant-id>` | Entra tenant under test | `AZURE_TENANT_ID` |
| `TB_SUBSCRIPTION_ID` | `<subscription-id>` | Azure subscription for the rig | `AZURE_SUBSCRIPTION_ID` |
| `TB_LOCATION` | `<region-with-gpt-4o-mini-capacity>` | Azure region for new resources | new deployment variable |
| `TB_RESOURCE_GROUP` | `tb-<resource-group>` | New isolated resource group | new deployment variable |
| `TB_LAW_RESOURCE_GROUP` | `<sentinel-resource-group>` | Existing workspace resource group | `LOG_ANALYTICS_RESOURCE_GROUP` |
| `TB_LAW_NAME` | `<sentinel-workspace-name>` | Existing Log Analytics/Sentinel workspace | `LOG_ANALYTICS_WORKSPACE_NAME` |
| `TB_LAW_CUSTOMER_ID` | `<workspace-id>` | Existing workspace customer ID | `LOG_ANALYTICS_WORKSPACE_ID` |
| `TB_LAW_RESOURCE_ID` | `/subscriptions/${TB_SUBSCRIPTION_ID}/resourceGroups/${TB_LAW_RESOURCE_GROUP}/providers/Microsoft.OperationalInsights/workspaces/${TB_LAW_NAME}` | Existing workspace ARM ID | `LOG_ANALYTICS_WORKSPACE_RESOURCE_ID` |
| `TB_PREFIX` | `tb` | Naming prefix | new deployment variable |
| `TB_FOUNDRY_ACCOUNT` | `${TB_PREFIX}-foundry-acct` | New Foundry/Azure AI Services account | new deployment variable |
| `TB_FOUNDRY_PROJECT` | `${TB_PREFIX}-foundry-proj` | New Foundry project | new deployment variable |
| `TB_MODEL_DEPLOYMENT` | `${TB_PREFIX}-gpt-4o-mini` | Pay-as-you-go model deployment | new deployment variable |
| `TB_MODEL_NAME` | `gpt-4o-mini` | Cheapest viable default model | new deployment variable |
| `TB_APP_INSIGHTS` | `${TB_PREFIX}-appi` | Workspace-based Application Insights component backed by `${TB_LAW_RESOURCE_ID}` | new deployment variable |
| `TB_ACA_ENV` | `${TB_PREFIX}-aca-env` | Shared Container Apps managed environment | new deployment variable |
| `TB_MCP_DELEGATED_APP` | `${TB_PREFIX}-mcp-delegated` | Container App for delegated path | new deployment variable |
| `TB_MCP_APPONLY_APP` | `${TB_PREFIX}-mcp-apponly` | Container App for app-only path | new deployment variable |
| `TB_DELEGATED_APP_REG_NAME` | `${TB_PREFIX}-mcp-delegated-appreg` | Entra app registration for delegated/OBO path | new deployment variable |
| `TB_APPONLY_MI_NAME` | `${TB_PREFIX}-mcp-apponly-mi` | User-assigned managed identity for app-only path | new deployment variable |
| `TB_DELEGATED_TARGET_GROUP_ID` | `<created-by-human>` | Disposable group for scenario A | investigation target input |
| `TB_APPONLY_TARGET_GROUP_ID` | `<created-by-human>` | Disposable group for scenario B | investigation target input |
| `TB_COPILOT_AGENT_ID` | `<created-by-human>` | Copilot/M365 agent identifier | evidence filter only |
| `TB_FOUNDRY_AGENT_ID` | `<deployment-output>` | Foundry agent ID | evidence filter only |
| `TB_ALLOWED_GRAPH_ACTION` | `PATCH /groups/{id}` | Harmless validation action | design constant |

`trackback/adapter.py` already consumes only the workspace variables (`AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `LOG_ANALYTICS_RESOURCE_GROUP`, `LOG_ANALYTICS_WORKSPACE_NAME`, `LOG_ANALYTICS_WORKSPACE_ID`, `LOG_ANALYTICS_WORKSPACE_RESOURCE_ID`). New deployment automation should keep every other `TB_*` variable outside TraceBack unless the application later needs explicit resource filters.

## 2. Resource inventory

### 2.1 Core resources

| Resource | Name | Notes |
|---|---|---|
| Resource group | `${TB_RESOURCE_GROUP}` | New and isolated. Delete this group to remove almost all rig resources. |
| Foundry/Azure AI Services account | `${TB_FOUNDRY_ACCOUNT}` | New account under `Microsoft.CognitiveServices/accounts`. Enable only required diagnostics. |
| Foundry project | `${TB_FOUNDRY_PROJECT}` | New project under the account. Connect to `${TB_APP_INSIGHTS}`. |
| Model deployment | `${TB_MODEL_DEPLOYMENT}` using `${TB_MODEL_NAME}` | Use Global Standard or regional pay-as-you-go, not PTU/reserved throughput. Default `gpt-4o-mini`; if unavailable, choose the cheapest current small chat model available in `${TB_LOCATION}`. |
| Application Insights | `${TB_APP_INSIGHTS}` | Workspace-based, backed by `${TB_LAW_RESOURCE_ID}` so traces land in the same workspace queried by TraceBack. |
| Container Apps managed environment | `${TB_ACA_ENV}` | Consumption workload profile; logs destination set to Azure Monitor. |
| MCP Container App A | `${TB_MCP_DELEGATED_APP}` | Delegated/OBO path only. Scale min replicas 0. |
| MCP Container App B | `${TB_MCP_APPONLY_APP}` | Application/managed identity path only. Scale min replicas 0. |
| Delegated identity | `${TB_DELEGATED_APP_REG_NAME}` | App registration + service principal configured for delegated Graph permission/OBO. Exact consent model is a security-review dependency. |
| Application identity | `${TB_APPONLY_MI_NAME}` | User-assigned managed identity preferred for Scenario B. Assign only the required Microsoft Graph app role for the disposable action. |
| Disposable targets | `${TB_DELEGATED_TARGET_GROUP_ID}`, `${TB_APPONLY_TARGET_GROUP_ID}` | Two empty cloud-only security groups, one per scenario. |

### 2.2 Why two Container Apps, not one app with two revisions/routes

Use **two Container Apps in one managed environment**. This is slightly more verbose but better evidence:

- `ContainerAppHTTPLogs.ContainerAppName` cleanly separates delegated vs app-only requests.
- Each app can bind a different identity and different environment variables with no revision-routing ambiguity.
- Scaling, revisions, and crash telemetry stay isolated.
- Consumption scale-to-zero keeps the cost difference negligible.

One app with two revisions/routes is not recommended because `RevisionName` and route weights become an extra correlation dependency and can blur identity separation during retries or rollout.

## 3. Scenario A: delegated chain and evidence map

Target chain:

`H1 human -> H2 Copilot/agent -> H3 Foundry agent -> H4 delegated MCP tool -> H5 delegated user token -> H6 Microsoft Graph PATCH -> H7 disposable group`

| Edge/hop | Required evidence | Table and fields | Join to adjacent hop | Semantics |
|---|---|---|---|---|
| H1 human | Human actor in M365 agent activity | `CopilotActivity.ActorUserId`, `ActorName`, `RecordId`, `TimeGenerated` | H1 and H2 are usually the same `CopilotActivity.RecordId`; if available, also `LLMEventData.ConversationId`, `ThreadId`, `Messages[].Id` | Exact if same record or native conversation/thread/message ID. |
| H2 Copilot agent | Copilot agent invocation | `CopilotActivity.AgentId`, `AgentName`, `AppIdentity`, `LLMEventData.PlatformAgentId`, `TargetAgentName`, `TargetPlatformAgentId`, `ConversationId`, `ThreadId` | To H3 by native conversation/thread/message/task/trace ID if Copilot passes one to Foundry and Foundry emits it; otherwise time window + known agent identifier only | Candidate only without an exact shared ID. |
| H3 Foundry agent | Foundry agent invocation trace | Primary: `AppRequests` / `AppDependencies` / `AppTraces` from `${TB_APP_INSIGHTS}` with `OperationId`, `Id`, `ParentId`, `Properties["gen_ai.agent.id"]`, `Properties["gen_ai.conversation.id"]`, `_ResourceId`, `AppRoleName` | To H4 by `OperationId` and parent/child span relation (`AppRequests.Id` = child `ParentId`, or shared W3C trace ID in `Properties.trace_id`) | Exact when span lineage is present. |
| H4 MCP/tool | Delegated MCP endpoint request | `AppRequests` for the MCP app if OTel/App Insights is enabled in the MCP server; native fallback `ContainerAppHTTPLogs` with `ContainerAppName`, `RevisionName`, `Method`, `Path`, `StatusCode`, `RequestId`, `StartTime`, `RequestDuration` | To H5 by exact SDK/HTTP dependency telemetry if emitted (`AppDependencies.Id`/`ParentId`/`OperationId`), otherwise time window + Container App name + identity | Native ACA HTTP logs alone do not prove token acquisition. |
| H5 workload identity | Delegated user token/sign-in | `AADNonInteractiveUserSignInLogs.UniqueTokenIdentifier`, `UserId`, `AppId`, `ServicePrincipalId`, `OriginalRequestId`, `CorrelationId`, `ResultType` | To H6 exact: `AADNonInteractiveUserSignInLogs.UniqueTokenIdentifier = MicrosoftGraphActivityLogs.SignInActivityId` | Exact token/sign-in join. |
| H6 Graph action | Graph PATCH with delegated permission | `MicrosoftGraphActivityLogs.RequestId`, `ClientRequestId`, `OperationId`, `SignInActivityId`, `AppId`, `ServicePrincipalId`, `UserId`, `Scopes`, `Roles`, `RequestMethod`, `RequestUri`, `ResponseStatusCode` | To H7 exact target object ID in `RequestUri`; to H5 exact token join above | Complete H6 only if `Scopes` nonempty, `Roles` empty, `UserId` equals delegated user. |
| H7 target | Disposable group changed | Same successful `MicrosoftGraphActivityLogs` row; optional `AuditLogs.TargetResources[].id`, `OperationName`, `CorrelationId` | Same Graph request contains `${TB_DELEGATED_TARGET_GROUP_ID}` | Exact for Graph URI; AuditLogs are corroboration unless exact documented ID matches. |

## 4. Scenario B: application chain and evidence map

Target chain:

`H1 human -> H2 Copilot/agent -> H3 Foundry agent -> H4 app-only MCP tool -> H5 managed identity/service principal -> H6 Microsoft Graph PATCH -> H7 disposable group`

| Edge/hop | Required evidence | Table and fields | Join to adjacent hop | Semantics |
|---|---|---|---|---|
| H1 human | Human actor in M365 agent activity | `CopilotActivity.ActorUserId`, `RecordId`, `ConversationId`, `ThreadId`, message IDs | H1/H2 same record or native Copilot identifiers | Exact inside Copilot record. |
| H2 Copilot agent | Agent handoff to Foundry | `CopilotActivity.AgentId`, `AppIdentity`, `TargetPlatformAgentId`, `TargetAgentName`, `ConversationId`, `ThreadId` | To H3 only exact if Foundry trace carries the same native conversation/thread/task/trace identifier | Without this, human-to-Foundry is a candidate only. |
| H3 Foundry agent | Foundry agent invocation | `AppRequests` / `AppDependencies` / `AppTraces`: `OperationId`, `Id`, `ParentId`, `Properties["gen_ai.agent.id"]`, `Properties["gen_ai.conversation.id"]`, trace/span IDs | To H4 by `OperationId` + span parent/child | Exact if trace lineage exists. |
| H4 MCP/tool | App-only MCP endpoint request | Prefer `AppRequests`/`AppDependencies` from MCP OTel; native fallback `ContainerAppHTTPLogs.ContainerAppName == ${TB_MCP_APPONLY_APP}`, `RequestId`, `Path`, `StatusCode`, `RevisionName` | To H5 by token request dependency/sign-in correlation if emitted; otherwise time + app identity only | Usually partial unless MCP telemetry captures dependency lineage. |
| H5 workload identity | Managed identity or service-principal token | Preferred MI: `AADManagedIdentitySignInLogs.UniqueTokenIdentifier`, `ServicePrincipalId`, `AppId`, `ResourceServicePrincipalId`, `ResultType`. If app registration: `AADServicePrincipalSignInLogs` same key set | To H6 exact: `<sign-in>.UniqueTokenIdentifier = MicrosoftGraphActivityLogs.SignInActivityId` | Exact Graph-token join. |
| H6 Graph action | Graph PATCH with application permission | `MicrosoftGraphActivityLogs.SignInActivityId`, `AppId`, `ServicePrincipalId`, `UserId`, `Scopes`, `Roles`, `RequestUri`, `RequestMethod`, `ResponseStatusCode` | To H7 exact target ID in URI | Complete app-only H6 if `Roles` nonempty, `Scopes` empty, and `UserId` empty. |
| H7 target | Disposable group changed | Same Graph row; optional `AuditLogs` | Same Graph request contains `${TB_APPONLY_TARGET_GROUP_ID}` | Exact target only from Graph row. |

Application-permission attribution to a human is complete only if H1->H2->H3->H4->H5 are linked by exact native IDs. If any upstream edge collapses to time + app identity, TraceBack should report `partial` or `unresolved` even though H5->H6->H7 is exact.

## 5. Required diagnostics and telemetry paths

### 5.1 Foundry account/project diagnostics

Enable these diagnostic settings to `${TB_LAW_RESOURCE_ID}`:

| Resource | Diagnostic category | Destination table | Required? | Why |
|---|---|---|---|---|
| `${TB_FOUNDRY_ACCOUNT}` (`Microsoft.CognitiveServices/accounts`) | `Audit` | `AzureDiagnostics` | Yes | Control-plane/account audit context. |
| `${TB_FOUNDRY_ACCOUNT}` | `RequestResponse` | `AzureDiagnostics` | Yes | Model/account request-response envelope for model calls. Do not enable body/content capture beyond native category behavior. |
| `${TB_FOUNDRY_ACCOUNT}` | `AzureOpenAIRequestUsage` | `AzureDiagnostics` | Yes | Token usage/cost evidence for the model deployment. |
| `${TB_FOUNDRY_ACCOUNT}` | `Trace` | `AzureDiagnostics` | Optional for troubleshooting | Not the primary H3 trace source, but useful if available. |
| `${TB_FOUNDRY_PROJECT}` (`Microsoft.CognitiveServices/accounts/projects`) | `Audit` | `AzureDiagnostics` | Yes | Project-level audit context. |
| `${TB_FOUNDRY_PROJECT}` | `Trace` | `AzureDiagnostics` | Yes | Project-level trace category. |
| `${TB_FOUNDRY_ACCOUNT}` / `${TB_FOUNDRY_PROJECT}` | `AllMetrics` | `AzureMetrics` | Yes | Basic health/cost correlation. |

Microsoft's supported-log-category pages currently list `Microsoft.CognitiveServices/accounts` categories `Audit`, `AzureOpenAIRequestUsage`, `ManagedNetworkEvent`, `RequestResponse`, and `Trace`, all landing in `AzureDiagnostics`; the project resource type lists `Audit` and `Trace`, also landing in `AzureDiagnostics`.

**Important:** TraceBack's current allowlist does not query `AzureDiagnostics`, so these categories make Foundry/account diagnostics visible in the workspace but do not by themselves satisfy H3 for the current product.

### 5.2 Foundry agent trace telemetry - primary H3

Connect `${TB_APP_INSIGHTS}` to `${TB_FOUNDRY_PROJECT}`. Foundry server-side tracing starts after the project is connected and stores traces in the connected Application Insights resource. This is the primary H3 path.

Expected tables and key fields:

- `AppRequests`: `OperationId`, `Id`, `ParentId`, `OperationName`, `Name`, `Properties`, `AppRoleName`, `_ResourceId`, `TimeGenerated`.
- `AppDependencies`: `OperationId`, `Id`, `ParentId`, `Name`, `Target`, `DependencyType`, `ResultCode`, `Properties`.
- `AppTraces`, `AppEvents`, `AppExceptions`: supporting events sharing `OperationId` / `ParentId` / `Properties`.
- GenAI attributes to look for in `Properties`: `gen_ai.agent.id`, `gen_ai.conversation.id`, `trace_id`, `span_id`.

Do not enable optional prompt/response content capture for this validation unless the coordinator separately approves it.

### 5.3 Container Apps diagnostics - primary native H4 visibility

At the **managed environment** `${TB_ACA_ENV}`, set logs destination to Azure Monitor and create a diagnostic setting to `${TB_LAW_RESOURCE_ID}` with:

| Diagnostic category | Destination table | Required? | Why |
|---|---|---|---|
| `ContainerAppHTTPLogs` | `ContainerAppHTTPLogs` | Yes | One row per ingress HTTP request with method/path/status/duration and `RequestId`. This is opt-in and disabled by default. |
| `ContainerAppConsoleLogs` | `ContainerAppConsoleLogs` | Yes | stdout/stderr from the app containers and Dapr sidecars. Keep application logging minimal and non-sensitive. |
| `ContainerAppSystemLogs` | `ContainerAppSystemLogs` | Yes | Revision, Keda, Dapr, Envoy, provisioning and platform events. |
| `AllMetrics` | `AzureMetrics` | Yes | Scaling, request, and resource metrics. |

At the **individual Container App** resources, diagnostic settings support metrics only; enable `AllMetrics` for `${TB_MCP_DELEGATED_APP}` and `${TB_MCP_APPONLY_APP}`. The log categories belong at managed-environment scope.

Expected `ContainerAppHTTPLogs` fields for H4 manual/native evidence: `TimeGenerated`, `StartTime`, `EnvironmentName`, `ContainerAppName`, `RevisionName`, `Method`, `Path`, `StatusCode`, `RequestId`, `RequestDuration`, `UpstreamHost`, `_ResourceId`. Treat `Path` and `XForwardedFor` as sensitive; do not export them into durable reports unless minimized.

### 5.4 MCP application telemetry - required for current TraceBack completeness

Container Apps native tables prove the H4 host saw a request, but current `trackback/queries.py` only consumes `AppRequests`, `AppDependencies`, `AppTraces`, `AppEvents`, and `AppExceptions` for H3/H4. Therefore the MCP implementation must emit Application Insights/OpenTelemetry telemetry into `${TB_APP_INSIGHTS}` or another workspace-based App Insights component backed by `${TB_LAW_RESOURCE_ID}`.

Minimum accepted MCP App Insights records:

- `AppRequests.OperationName` or `Name` equals one of TraceBack's accepted MCP names: `tools/call`, `mcp.tool.call`, or `mcp.tools.call`.
- `AppRequests.OperationId`, `Id`, `ParentId` preserve W3C trace/span lineage from Foundry when possible.
- Outbound Graph call appears as `AppDependencies` with the same `OperationId` and a parent-child relationship to the MCP request.
- No prompt, response body, token, client secret, Authorization header, full query string, or complete claims bag is logged.

This is the main dependency on the MCP server.

### 5.5 Entra and Graph telemetry already required

Retain the existing tenant diagnostics into `${TB_LAW_RESOURCE_ID}`:

- `CopilotActivity`
- `SigninLogs`
- `AADNonInteractiveUserSignInLogs`
- `AADServicePrincipalSignInLogs`
- `AADManagedIdentitySignInLogs`
- `AuditLogs`
- `MicrosoftGraphActivityLogs`

Critical join remains:

```text
MicrosoftGraphActivityLogs.SignInActivityId = <sign-in table>.UniqueTokenIdentifier
```

## 6. Deployment plan - design only

1. Confirm `${TB_LOCATION}` has quota/capacity for `${TB_MODEL_NAME}` as pay-as-you-go, not PTU.
2. Create `${TB_RESOURCE_GROUP}` with tags: `purpose=trackback-test-rig`, `owner=${TB_OWNER}`, `expires=${TB_EXPIRATION_DATE}`.
3. Create `${TB_APP_INSIGHTS}` as workspace-based Application Insights pointing to `${TB_LAW_RESOURCE_ID}`.
4. Create `${TB_FOUNDRY_ACCOUNT}` and `${TB_FOUNDRY_PROJECT}`.
5. Deploy `${TB_MODEL_DEPLOYMENT}` using `${TB_MODEL_NAME}` and lowest pay-as-you-go deployment option.
6. Connect `${TB_APP_INSIGHTS}` to `${TB_FOUNDRY_PROJECT}` for tracing.
7. Enable Foundry account/project diagnostic settings listed in section 5.1 to `${TB_LAW_RESOURCE_ID}`.
8. Create `${TB_ACA_ENV}` in consumption mode with logs destination `azure-monitor`.
9. Enable managed-environment diagnostic categories `ContainerAppHTTPLogs`, `ContainerAppConsoleLogs`, `ContainerAppSystemLogs`, plus `AllMetrics`, to `${TB_LAW_RESOURCE_ID}`.
10. Create `${TB_MCP_DELEGATED_APP}` with min replicas `0`, max replicas `1`, identity/config for delegated/OBO, and App Insights/OTel export to `${TB_APP_INSIGHTS}`.
11. Create `${TB_MCP_APPONLY_APP}` with min replicas `0`, max replicas `1`, `${TB_APPONLY_MI_NAME}`, and App Insights/OTel export to `${TB_APP_INSIGHTS}`.
12. Create or link one Foundry agent per scenario, or one Foundry agent with two explicit tools whose paths map to the two Container Apps. Prefer one Foundry project and one model deployment; use separate MCP tools/endpoints for telemetry separation.
13. Create the M365/Copilot front door that invokes the Foundry agent. This likely requires web UI work by the human.
14. Run non-mutating smoke tests first and verify H3/H4 rows before any Graph mutation.
15. Run Scenario A and Scenario B in separate quiet windows using separate disposable groups.

## 7. Cost estimate

Target: under USD $100/month; expected: under USD $20/month for low-volume validation.

| Resource/cost driver | Estimate | Control |
|---|---:|---|
| Container Apps compute | ~$0 for idle; likely within monthly free grant for sparse tests | Consumption plan, min replicas `0`, max replicas `1`; first monthly grants cover small usage. |
| Container Apps requests | ~$0 for sparse tests | First monthly request grant should cover validation traffic. |
| Application Insights / Log Analytics ingestion | $0-$5/month expected | Keep logs low volume; avoid verbose body/content logging; existing Graph volume is already negligible. |
| Foundry/Azure OpenAI model tokens | Usually <$1 for validation | `gpt-4o-mini` pay-as-you-go. At example pricing of about $0.15/1M input and $0.60/1M output tokens, 100 runs at 5k input + 1k output is about $0.075. Verify current Azure regional price before deployment. |
| Foundry account/project | $0 fixed expected | Consumption billing is mainly model tokens/logs; no PTU. |
| Application Insights component | $0 fixed expected | Billed through Log Analytics ingestion/retention. |
| Managed identities / app registrations / security groups | $0 | No direct charge. |
| MicrosoftGraphActivityLogs incremental ingestion | likely pennies for this rig | Existing measured volume is 0.24 MB/7d; rig adds a handful of rows. |

Guardrails:

- No provisioned throughput units.
- No always-on Container App replicas.
- No content-heavy trace capture.
- Set workspace daily cap/alerts outside this design if the owner wants a hard spend brake.

## 8. Human-required actions

Keep this list short and explicit:

1. Pick the Azure region for the new rig and confirm the subscription can deploy `gpt-4o-mini` there.
2. Click the admin-consent prompts for the delegated app and the app-only/managed-identity Graph permission. Use only the disposable group permission scope security review approves.
3. Create the two empty disposable security groups and give us their object IDs.
4. In Copilot Studio / M365 agent UI, create or wire the test Copilot agent so it calls the new Foundry agent. This usually cannot be done fully headlessly.
5. Connect the Foundry project to the Application Insights resource if the portal requires an interactive confirmation.
6. During each test, sign in as the dedicated test user, send exactly one prompt, and do not retry in the same quiet window.
7. After evidence is captured, restore the disposable group descriptions and confirm rollback.

## 9. Uncertainty and known limits

- **H2 -> H3 may remain unresolved** if CopilotActivity and Foundry traces do not share a native conversation, message, task, operation, or trace ID. Same user + same time + same agent name is not enough.
- **H4 -> H5 may remain partial** if MCP/SDK telemetry does not expose a token request, dependency, or correlation identifier that links the MCP request to the Entra sign-in row.
- **Application permissions cannot identify the human from Graph alone.** Graph will show `AppId`/`ServicePrincipalId` and `Roles`, not the original requester.
- **ContainerAppHTTPLogs are host evidence, not full causal proof.** They show ingress requests; they do not automatically link to token acquisition or Graph calls.
- **AuditLogs are corroboration, not a replacement for MicrosoftGraphActivityLogs.** They can show a directory effect, but not the Graph request envelope, permission mode, or `SignInActivityId`.
- **Telemetry latency can make a valid run temporarily inconclusive.** Wait for documented ingestion windows before declaring failure.
- **If multiple validation invocations overlap, attribution becomes ambiguous.** Run one scenario at a time against quiet targets.

## 10. Teardown command list

Use only after the coordinator authorizes cleanup. These are variable-based examples, not commands to run now.

```powershell
# Remove the isolated Azure resource group and all Azure resources in it.
az group delete --subscription $env:TB_SUBSCRIPTION_ID --name $env:TB_RESOURCE_GROUP --yes

# Remove app registration/service principal created for delegated path.
$delegatedAppId = az ad app list --display-name $env:TB_DELEGATED_APP_REG_NAME --query "[0].appId" -o tsv
if ($delegatedAppId) { az ad app delete --id $delegatedAppId }

# Remove user-assigned managed identity service principal app-role assignments if not removed with the resource group.
# Security review should supply the exact Graph app-role assignment deletion commands for the chosen identity model.

# Delete disposable groups after rollback is independently verified.
az ad group delete --group $env:TB_DELEGATED_TARGET_GROUP_ID
az ad group delete --group $env:TB_APPONLY_TARGET_GROUP_ID

# Optional verification: no tb- resources remain in the subscription.
az resource list --subscription $env:TB_SUBSCRIPTION_ID --query "[?starts_with(name, 'tb-')].{name:name,type:type,resourceGroup:resourceGroup}" -o table
```

## 11. Documentation references verified on 2026-09-19

- Foundry diagnostic logging: https://learn.microsoft.com/en-us/azure/foundry/how-to/diagnostic-logging
- Foundry agent tracing setup: https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup
- Foundry agent tracing concepts: https://learn.microsoft.com/en-us/azure/foundry/observability/concepts/trace-agent-concept
- Cognitive Services account supported logs: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/supported-logs/microsoft-cognitiveservices-accounts-logs
- Cognitive Services account/project supported logs: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/supported-logs/microsoft-cognitiveservices-accounts-projects-logs
- Application Insights `AppRequests`: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/apprequests
- Application Insights `AppDependencies`: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/appdependencies
- Container Apps logging: https://learn.microsoft.com/en-us/azure/container-apps/logging
- Container Apps log options/diagnostic settings: https://learn.microsoft.com/en-us/azure/container-apps/log-options
- Container Apps managed-environment tables: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/microsoft-app_managedenvironments
- `ContainerAppHTTPLogs`: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/containerapphttplogs
- `ContainerAppConsoleLogs`: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/containerappconsolelogs
- `ContainerAppSystemLogs`: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/containerappsystemlogs
- Azure Monitor costs: https://learn.microsoft.com/en-us/azure/azure-monitor/usage-estimated-costs
- Azure Container Apps pricing: https://azure.microsoft.com/en-us/pricing/details/container-apps/
- Azure OpenAI pricing: https://azure.microsoft.com/en-us/pricing/details/azure-openai/
- Microsoft Graph activity logs overview: https://learn.microsoft.com/en-us/graph/microsoft-graph-activity-logs-overview
- `MicrosoftGraphActivityLogs` table: https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/microsoftgraphactivitylogs
