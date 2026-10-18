# Analyst pivot: who ultimately caused this API action?

TraceBack API responses deliberately contain only opaque references
(`corr-…`, `evidence-…`). An analyst with Sentinel read access uses the queries
below to see the actual identities behind a TraceBack result. Every join is on
an exact identifier, never on time proximity, except queries 3 and 3b, which
are explicitly labeled candidate lists.

Set `target` to the UPN or object ID that was acted on.

## Evidence tiers

| Tier | Meaning | Delegated (OBO) | App-only |
| --- | --- | --- | --- |
| 1. Native, exact | Microsoft-generated logs joined by a token or request ID | Graph `UserId` + `SignInActivityId` = sign-in `UniqueTokenIdentifier` names the human and the client app | Names only the workload identity |
| 2. Native, candidate | Microsoft-generated logs that bound *who could have* done it, but are not joined by an ID | Not needed | `CopilotInteraction` shows which users prompted the agent that uses this workload |
| 3. Self-reported | Logs written by the MCP server you operate (`graph_client_request_id` = Graph `ClientRequestId`) | Corroborates tier 1 | The only per-call link to an inbound user, and only if the caller passes one (it currently does not) |

Only tier 1 counts as attribution. Tier 2 narrows the investigation. Tier 3 is
trustworthy only as far as the MCP host and its logging pipeline are trusted.

## 1. Last hop: who presented the token for each action on the target

```kusto
let target = "user1@contoso.com";
let lookback = 7d;
let target_oid = toscalar(union isfuzzy=true (datatable(AccountObjectId:string)[]),
    (IdentityInfo | where TimeGenerated > ago(14d) and AccountUPN =~ target
     | project AccountObjectId = tostring(AccountObjectId))
    | where isnotempty(AccountObjectId) | summarize take_any(AccountObjectId));
let user_tokens = union isfuzzy=true AADNonInteractiveUserSignInLogs, SigninLogs
    | where TimeGenerated > ago(lookback + 1d) and isnotempty(UniqueTokenIdentifier)
    | summarize arg_max(TimeGenerated, UserPrincipalName, UserId, AppDisplayName, IPAddress, SessionId) by UniqueTokenIdentifier
    | project UniqueTokenIdentifier, TokenUser = UserPrincipalName, TokenUserId = UserId,
        TokenApp = AppDisplayName, SignInIp = IPAddress, SessionId;
let workload_tokens = union isfuzzy=true AADManagedIdentitySignInLogs, AADServicePrincipalSignInLogs
    | where TimeGenerated > ago(lookback + 1d) and isnotempty(UniqueTokenIdentifier)
    | summarize arg_max(TimeGenerated, ServicePrincipalName, ServicePrincipalId) by UniqueTokenIdentifier
    | project WorkloadToken = UniqueTokenIdentifier, WorkloadName = ServicePrincipalName, WorkloadId = ServicePrincipalId;
MicrosoftGraphActivityLogs
| where TimeGenerated > ago(lookback)
| where RequestUri has target or RequestUri has url_encode_component(target)
    or (isnotempty(target_oid) and RequestUri has target_oid)
| extend Mode = case(isnotempty(Scopes) and isnotempty(Roles), "mixed",
    isnotempty(Scopes), "delegated", isnotempty(Roles), "application", "unknown")
| join kind=leftouter user_tokens on $left.SignInActivityId == $right.UniqueTokenIdentifier
| join kind=leftouter workload_tokens on $left.SignInActivityId == $right.WorkloadToken
| extend Binding = case(
    Mode == "delegated" and TokenUserId == UserId, "user: exact token match",
    Mode == "delegated" and isnotempty(TokenUserId), "CONFLICT: token user differs",
    Mode == "delegated", "user: Graph log only",
    Mode == "application" and WorkloadId == ServicePrincipalId, "workload: exact token match",
    Mode == "application", "workload: Graph log only", "unresolved")
| project TimeGenerated, RequestMethod, RequestUri, ResponseStatusCode, Mode, Binding,
    Human = iff(Mode == "delegated", coalesce(TokenUser, UserId), "(not in API telemetry)"),
    ClientApp = coalesce(TokenApp, AppId), Workload = coalesce(WorkloadName, ServicePrincipalId),
    SignInActivityId, ClientRequestId, IPAddress, SignInIp, SessionId
| order by TimeGenerated desc
```

`SessionId` pivots to the user's interactive sign-in and device, which is
useful when you need to know where the request originated.

## 2. MCP hop (self-reported, schema v2)

```kusto
let lookback = 7d;
ContainerAppConsoleLogs
| where TimeGenerated > ago(lookback)
| extend e = parse_json(coalesce(tostring(column_ifexists("Log", "")), tostring(column_ifexists("Log_s", ""))))
| where tostring(e.event) == "graph_request"
| project McpTime = TimeGenerated, ContainerAppName, Tool = tostring(e.tool_name),
    AuthMode = tostring(e.auth_mode), ClientRequestId = tostring(e.graph_client_request_id),
    InboundUser = tostring(e.token_claims.oid), InboundTokenId = tostring(e.token_claims.uti),
    OutboundTokenId = tostring(e.outbound_token_claims.uti), Traceparent = tostring(e.platform_ids.traceparent),
    InboundHeaders = e.inbound_header_names
| join kind=leftouter (MicrosoftGraphActivityLogs | where TimeGenerated > ago(lookback)
    | project ClientRequestId, GraphUser = UserId, GraphSp = ServicePrincipalId, SignInActivityId, RequestUri)
    on ClientRequestId
| extend SelfReportCheck = case(isempty(SignInActivityId), "no Graph row (filtered or not yet ingested)",
    isempty(OutboundTokenId), "pre-v2 MCP line", SignInActivityId == OutboundTokenId, "consistent", "MISMATCH")
| order by McpTime desc
```

`MISMATCH` means the MCP log and Microsoft's Graph log disagree about which
token was used. Investigate it as possible tampering or a bug.

## 3. App-only: candidate requesters (not proof)

App-only Graph calls carry no user. Microsoft 365 Copilot's audit still records
which users prompted the agent that is configured to call this workload. List
those agents in `agent_ids`; this is configuration knowledge, not telemetry.

```kusto
let agent_ids = dynamic(["T_<app-only-copilot-agent-id>"]);
let action_time = datetime(2026-10-09T20:11:06Z);
CopilotActivity
| where RecordType == "CopilotInteraction"
| extend Agent = tostring(LLMEventData.TargetPlatformAgentId)
| where Agent in (agent_ids) or AgentId has_any (agent_ids)
| where TimeGenerated between ((action_time - 30m) .. (action_time + 5m))
| summarize Prompts = count(), FirstPrompt = min(TimeGenerated), LastPrompt = max(TimeGenerated),
    Conversations = make_set(tostring(LLMEventData.ConversationId), 10) by ActorUserId, Agent
| join kind=leftouter (IdentityInfo | where TimeGenerated > ago(14d)
    | summarize arg_max(TimeGenerated, AccountUPN) by AccountObjectId = tostring(AccountObjectId)
    | project AccountObjectId, AccountUPN) on $left.ActorUserId == $right.AccountObjectId
| project-away AccountObjectId
```

One row does not prove that user caused the action. It means no other user
prompted that agent in the window. Treat it as a lead and confirm through
tier 1 or by interviewing the user.

## 3b. App-only: requester lead via Application Insights (not proof)

Requires Application Insights connected to the Foundry project. This is
stronger than query 3 because the agent is identified from logs, not from
configuration, and the window is anchored to the agent's own tool call.
TraceBack runs the same chain as `last_hop.requester_lead`.

```kusto
let target = "user1@contoso.com";
let lookback = 7d;
let tools = AppDependencies
| where TimeGenerated > ago(lookback)
| where tostring(Properties["gen_ai.operation.name"]) == "execute_tool"
| extend Args = tostring(Properties["gen_ai.tool.call.arguments"]), Result = tostring(Properties["gen_ai.tool.call.result"])
| where Args contains target or Args contains url_encode_component(target) or Result contains target
| project ToolStart = TimeGenerated, ToolEnd = TimeGenerated + DurationMs * 1ms, Tool = Name,
    Blueprint = tostring(Properties["microsoft.a365.agent.blueprint.id"]),
    AgentConversation = tostring(Properties["gen_ai.conversation.id"]), OperationId;
let agents = AADServicePrincipalSignInLogs
| where TimeGenerated > ago(lookback)
| extend Blueprint = tostring(parse_json(tostring(column_ifexists("Agent", ""))).parentAppId)
| where isnotempty(Blueprint)
| distinct Blueprint, AgentIdentity = ServicePrincipalId, AgentName = ServicePrincipalName;
let graph = MicrosoftGraphActivityLogs
| where TimeGenerated > ago(lookback)
| where RequestUri has target or RequestUri has url_encode_component(target)
| project GraphTime = TimeGenerated, RequestUri, GraphSp = ServicePrincipalId, GraphUser = UserId;
let prompts = CopilotActivity
| where TimeGenerated > ago(lookback) and RecordType == "CopilotInteraction"
| extend AgentIdentity = extract(@"([0-9a-fA-F-]{36})$", 1, AgentId)
| project PromptTime = TimeGenerated, AgentIdentity, ActorUserId, ActorUserType,
    CopilotConversation = tostring(LLMEventData.ConversationId);
graph
| extend k = 1 | join kind=inner (tools | extend k = 1) on k
| where GraphTime between ((ToolStart - 5s) .. (ToolEnd + 5s))   // target + time
| join kind=inner agents on Blueprint                            // exact
| join kind=leftouter prompts on AgentIdentity                   // exact
| where PromptTime between ((ToolStart - 15m) .. (ToolStart + 1m)) // agent + time
| project GraphTime, RequestUri, GraphSp, GraphUser, Tool, ToolStart, AgentName, AgentIdentity,
    AgentConversation, PromptTime, ActorUserId, ActorUserType, CopilotConversation
| order by GraphTime desc, PromptTime desc
```

One distinct `ActorUserId` per Graph row is a sole candidate. The Copilot
`ConversationId` and the agent's `gen_ai.conversation.id` are different IDs;
no Microsoft log joins them, which is why this stays a lead.

## Closing the app-only gap

Without an architecture change, app-only attribution cannot go beyond tier 2.
Options, in order of strength:

1. Use OBO (delegated) for user-initiated tools. Graph then records the user.
2. Use Entra Agent ID with user context (agent user / OBO for agents) so the
   agent identity and the human both appear in sign-in logs.
3. Ask the platform to log the Copilot conversation ID and the end user on
   the Foundry tool call. Foundry already sends MCP servers an
   `x-ms-foundry-baggage` header naming the calling agent identity, but no
   Microsoft log records it, and it carries no conversation or user.
4. If app-only is required, have the agent pass a platform-generated
   conversation or trace ID to the MCP tool so that tier 3 can bind it to
   `CopilotInteraction` `ConversationId`. Check `InboundHeaders` from query 2
   to see what Foundry already sends.
