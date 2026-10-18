# TraceBack test rig — resume here

Last updated: 2026-10-04

## Where we are

The test rig is **deployed and live**. The TraceBack app is built and passing 256 tests.
Remaining work is mostly human clicks, then the actual test run.

## Deployed rig (do not redeploy)

- Subscription: `<subscription-id>`
- Resource group: `tb-<resource-group>` (eastus2) — the ONLY `tb-` group; nothing stray
- Telemetry destination: workspace `<sentinel-workspace-name>`, RG `<sentinel-resource-group>`,
  customerId `<workspace-id>`
- Cost: ~$5/month fixed (ACR Basic). Container Apps are `minReplicas=0`, so idle is $0.

Resources: `tb-foundry-rig` (+ `tb-project-rig`, gpt-4o-mini),
`tb-appi-rig`, `tbacrrig`, `tb-aca-env-rig`,
`tb-mcp-delegated-rig`, `tb-mcp-apponly-rig`,
`tb-mcp-apponly-mi-rig` (user-assigned managed identity).

Health verified 2026-09-19 (both HTTP 200):

- delegated: `https://tb-mcp-delegated-rig.<aca-env-id>.eastus2.azurecontainerapps.io/health`
  -> `{"ready":"true","auth_mode":"delegated"}`
- app-only: `https://tb-mcp-apponly-rig.<aca-env-id>.eastus2.azurecontainerapps.io/health`
  -> `{"ready":"true","auth_mode":"application"}`

MCP endpoints (for the Foundry agents) are the same hosts with `/mcp`.

## Checklist

- [x] Step 1 — Admin consent for delegated app reg
      `tb-mcp-delegated-appreg-rig`, app ID `<delegated-app-id>`,
      Microsoft Graph **User.ReadBasic.All** (delegated). **DONE by user 2026-10-04.**
- [x] Step 2 — Grant Microsoft Graph **User.ReadBasic.All** (application) to the managed
      identity `tb-mcp-apponly-mi-rig`. Awkward in the portal; do it via CLI.
      **DONE 2026-10-04.** Verified app-role assignment
      `n4EiKBh9TE2krrfMmttXBCHb5nQGiUxIv5NNbqH74A4`.
- [x] Step 3 — Create two Microsoft Foundry prompt agents using model deployment
      `tb-gpt-4o-mini-rig`. **DONE 2026-10-04.**
      - Delegated: `tb-agent-delegated-rig` v1, using Foundry connection
        `tb-graph-delegated-rig` with custom OAuth to the MCP app and OBO exchange
        to Microsoft Graph. The OAuth credential expires 2027-01-02.
      - App-only: `tb-agent-apponly-rig` v1, calling the managed-identity MCP endpoint.
      Both agents allow only `get_user_by_upn`; tool approval is disabled because the isolated
      tool is read-only and performs exactly one Graph GET.
- [x] Step 4 — In the Foundry portal, open resource `tb-foundry-rig` (its child/API
      project is `tb-project-rig`) and run two agent prompts (e.g.
      "look up user1@contoso.com"), one agent at a time, a few minutes apart, no retries.
      Overlapping runs make time-window correlation ambiguous.
      - App-only: **PASSED 2026-10-04 around 14:52 local.**
      - Delegated: first attempt exposed the managed-token trust restriction. Custom OAuth/OBO
        was configured afterward. **PASSED 2026-10-04 around 15:18 local** after first-time
        user consent.
- [x] Step 5 — Publish both tested Foundry agents to **Teams and Microsoft Copilot**, scope
      **Just you**, so the stable Foundry endpoints are callable from M365 Copilot.
      **DONE 2026-10-04.** Both use F0 Bot Service registrations with public network access
      disabled, Teams channels enabled, Activity protocol enabled, and `BotServiceRbac`.
      - Delegated title ID: `T_<delegated-copilot-agent-id>70a4-1439-62e1-dcc3e73f72f9`
      - App-only title ID: `T_<app-only-copilot-agent-id>`
- [x] Step 6 — Repeat the delegated and app-only prompts from M365 Copilot, one at a time,
      a few minutes apart, with no retries. **DONE 2026-10-04.**
      - Delegated: **PASSED at 15:37 EDT.**
      - App-only: **PASSED at 15:42 EDT.**
- [x] Step 7 — Run TraceBack against the telemetry and present the Foundry-only and full
      M365 Copilot chains. **DONE 2026-10-04. Final classification: PASS-SAFETY-ONLY / partial
      for both modes; not PASS-E2E.** See the final native-only result below.

## Test parameters

- Actor: the user's own admin account (`user2@contoso.com`)
- Target: `user1@contoso.com` (verified to exist) — read-only lookup only

## Standing constraints

- Read-only Graph permissions only (`User.ReadBasic.All`). No write permission anywhere.
- No cloud resource may be created, modified, or deleted without explicit user authorization.
- Never touch resources outside `tb-<resource-group>`.
- No hardcoded tenant/subscription/workspace values — everything stays env-var driven.
- Do not read `.env*`. Never emit secrets, keys, or tokens.
- Cost cap: under USD $100/month, target under $20/month.
- Tell the user directly and in plain language whenever an answer, approval, or test action
  is needed from them.

## Known open question

`ContainerAppConsoleLogs` vs `ContainerAppConsoleLogs_CL` — the app defaults to the `_CL`
form (matches the existing workspace) and is configurable via
`TRACKBACK_TABLE_CONTAINER_APP_CONSOLE_LOGS`. Confirm which the new rig actually emits once
test traffic has flowed.

## Honest caveat to keep in view

The Copilot -> Foundry hop may not share a native platform identifier. If it does not, that
hop will be reported as `unresolved` / heuristic rather than guessed. That is a legitimate
finding about where Microsoft telemetry breaks attribution today, not a bug to paper over.

## Final native-only validation result

The native Graph audit check was repeated after the documented ingestion allowance. It still returned no matching `GET` rows for either test identity or the target. Diagnostic-only MCP Application Insights dependencies confirm that both Graph lookups returned HTTP 200, but they are application instrumentation and are not attribution evidence.

### Delegated permissions

| Adjacent link | Native evidence | Result |
| --- | --- | --- |
| H1 human → H2 M365 agent | Copilot audit records the initiating human, delegated title ID, and conversation | exact |
| H2 M365 agent → H3 Foundry agent | Published agent ID suffix matches the Foundry agent identity | exact |
| H3 Foundry agent → H4 MCP service | Foundry and Container Apps rows exist, but share no invocation identifier | unresolved |
| H4 MCP service → H5 delegated identity | Dedicated MCP traffic and successful user/app Graph sign-ins exist without a request-level join | candidate |
| H5 delegated identity → H6 Graph action | No matching native Graph activity `GET` row | unavailable |
| H6 Graph action → H7 target | No matching native Graph activity `GET` row | unavailable |

### Application permissions

| Adjacent link | Native evidence | Result |
| --- | --- | --- |
| H1 human → H2 M365 agent | Copilot audit records the initiating human, app-only title ID, and conversation | exact |
| H2 M365 agent → H3 Foundry agent | Published agent ID suffix matches the Foundry agent identity | exact |
| H3 Foundry agent → H4 MCP service | Foundry and Container Apps rows exist, but share no invocation identifier | unresolved |
| H4 MCP service → H5 managed identity | Dedicated MCP traffic and a successful managed-identity Graph sign-in exist without a request-level join | candidate |
| H5 managed identity → H6 Graph action | No matching native Graph activity `GET` row | unavailable |
| H6 Graph action → H7 target | No matching native Graph activity `GET` row | unavailable |

The final TraceBack rerun returned HTTP 200 with `status=partial`, `confidence=none`, and `human_attribution=unresolved` for both modes. Delegated reasons were `persisted_user_required` and `incomplete_coverage`; application reasons were `backward_human_link_required` and `incomplete_coverage`. Its target-seeded query plan did not surface the manually located upstream native rows, so all seven normalized hops remained unavailable. This is a query-coverage limitation to address separately, not a reason to weaken exact-link requirements.

**Final classification: PASS-SAFETY-ONLY / partial for delegated and application permissions.** The controlled read-only actions worked, and TraceBack avoided false human attribution, but native telemetry did not support either full Foundry-only or full M365 Copilot end-to-end lineage.

## 2026-10-07 product improvements and retest

TraceBack now:

- matches the affected target only against resource-oriented fields, rather than
  incorrectly treating the target object ID as an actor or application ID;
- searches `AppDependencies` name and target fields for bounded discovery;
- allows validated non-evidence rows to contribute query keys without returning
  them as attribution evidence;
- treats custom structured MCP console events as diagnostic/discovery-only;
- accepts Azure CLI's string `"False"` representation for the KQL
  `InvalidIdentifier` flag while rejecting true or malformed values;
- prioritizes backward discovery before Foundry and Copilot queries; and
- retries one transient Azure CLI connection reset within the existing total
  investigation budget.

The full suite passes with 271 tests. A live rerun now observes three native
Graph rows for the target, but they are unrelated application-permission POST
actions from October 1, 3, and 6. The original October 4 delegated and
application test GET rows remain absent.

Required retest: run one fresh delegated lookup and, several minutes later, one
fresh app-only lookup from Microsoft 365 Copilot. Record both completion times,
do not retry either prompt, and do not run other activity against the target
during the window.

## 2026-10-10 root cause and attribution hardening

**Root cause of the missing Graph evidence.** The workspace default DCR
`trackback-graph-workspace-transform` (created 2026-08-29) kept only
`/identityProtection/`, `/riskyUsers/`, and POST/PATCH/DELETE on `/users`. Every
Graph `GET /users/...`, including every MCP lookup, was dropped before ingestion.
It now also keeps all rows from the two MCP identities (AppId `<delegated-app-id>`,
`<managed-identity-client-id>`; SP `<managed-identity-principal-id>`). The original definition is backed up in the session
files folder.

**Oct 9 delegated run never reached MCP.** No OBO token issuance, no MCP HTTP or
console line, no Foundry request, and no Copilot audit row exist for it. Copilot
answered from built-in directory knowledge (that is why it returned fewer fields).
Only the app-only run executed the tool (Graph call 20:11:06Z).

**What native telemetry already proves (Oct 7 delegated):**
`AADNonInteractiveUserSignInLogs` shows OBO issuance to Microsoft Graph for
`UserId=<user2-id>` and client `<delegated-app-id>`, each with a `UniqueTokenIdentifier`.
Once Graph GETs are ingested, `Graph.SignInActivityId == UniqueTokenIdentifier`
binds the last hop to the human with no timing.

**Changes:**

- MCP server (image `20261010160131`, revisions `--0000006` / `--0000005`): sends a
  generated `client-request-id`; logs inbound and outbound token `uti`, outbound
  `oid/appid/idtyp`, token source, `traceparent`, and inbound header names.
- TraceBack: console source is `ContainerAppConsoleLogs` (the `_CL` default never
  existed here); searches percent-encoded UPN and `IdentityInfo` object ID; MCP
  token IDs and trace ID feed discovery; new `/api/coverage` self-check. 282 tests pass.

**Further hardening (same day):** `last_hop` verdict in API and UI; exact-match
row ranking and key seeding (a live run previously hit the 128-key limit and
marked every source truncated); `docs/ANALYST-PIVOT.md` with live-tested KQL. 291
tests pass. `CopilotActivity` (`CopilotInteraction`) is ingested and names the
human who prompted each TraceBack agent (admin, both agents, Oct 4–9). It has no
tool or downstream IDs, so it is a candidate source for app-only, not proof.

**Retest:** one delegated and one app-only M365 Copilot prompt that explicitly
invokes the tool (for example, "Use the TraceBack tool to look up
user1@contoso.com"). Then confirm Graph rows exist for both MCP identities,
delegated rows carry `UserId`, and `SignInActivityId` joins to the sign-in
`UniqueTokenIdentifier`. Check `inbound_header_names` for app-only to learn which
correlation headers Foundry sends.

## Teardown

`testrig/teardown.ps1` removes the whole rig. `deploy.ps1` writes the inventory to `testrig/deployment-state.json` (git-ignored).

## 2026-10-11: delegated "issue with the lookup" root cause (Entra 90009)

- Symptom: the delegated agent in M365 Copilot answers "It seems there was an issue with the lookup". MCP receives discovery calls but no `tools/call`, and no Graph request is made.
- Evidence: `AADNonInteractiveUserSignInLogs` shows error **90009** for client = resource = `<delegated-app-id>` with `IncomingTokenType=refreshToken`. The message is "Application is requesting a token for itself… supported only if resource is specified using the GUID based App Identifier". Retries sometimes worked because a later interactive sign-in succeeded.
- Cause: the Foundry connection `tb-graph-delegated-rig` requested scope `api://<delegated-app-id>/access_as_user`, and its OAuth client is the same app as the resource.
- Fix applied: changed the connection scope to the GUID form `<delegated-app-id>/access_as_user` (with `offline_access` unchanged), using an ARM PUT on the connection.
- Cleaner long-term option: use a separate OAuth client app registration for the connector, so client ≠ resource.

### Retest after the scope fix (2026-10-11 17:14Z delegated, 17:17Z app-only): PASS-E2E for both

- No 90009 errors after the fix, and both agents answered on the first try.
- **Delegated:** the Graph GET row has `UserId` = admin `<user2-id>`, and its `SignInActivityId` `<token-id>` equals the OBO sign-in `UniqueTokenIdentifier` (client `<delegated-app-id>`, resource Graph). It also equals MCP `outbound_token_claims.uti`, and Graph `ClientRequestId` equals MCP `graph_client_request_id`. TraceBack `last_hop`: `user_identified` / `token_exact`.
- **App-only:** the Graph GET row is from managed identity `<managed-identity-principal-id>`, and its `SignInActivityId` `<token-id>` equals the `AADManagedIdentitySignInLogs` UTI. TraceBack reports this read as `token_exact` workload. The overall verdict is `multiple_actors` because Microsoft first-party services (IAMTenantCrawler; Defender for Cloud Apps – Customer Experience) also POSTed against user1 in the window. The human is not present in API telemetry, as designed.
- Foundry sends `traceparent`, `baggage` and `x-ms-foundry-baggage` to MCP in both modes. Lead to follow up: capture the safe IDs from `x-ms-foundry-baggage`, which may bind app-only calls to a Foundry response or conversation, and through that to `CopilotInteraction`.
- TraceBack fixes from this retest:
  - `MatchRank` now has 4 tiers. Seeding is restricted to rank 0–1 plus small sessions, which removes `key_limit` and the hidden OBO row.
  - New reason codes `context_row_limit` and `self_reported_only`.
  - A query that times out is retried once.

### 2026-10-11 schema v3 baggage findings and agent-scoped requester bridge

Retest at 17:52Z (app-only) and 17:56Z (delegated) on image `20261011174413`: both PASS-E2E.

- `x-ms-foundry-baggage` carries `foundry.caller.agentic_identity.id` (app-only
  `<app-only-agent-identity-id>`, delegated `<delegated-agent-identity-id>`), the identity type (`agent`), agent name and
  version, and the Foundry account and project IDs. It has **no conversation, thread
  or user ID**. `baggage` carries only `leaf_customer_span_id`.
- The agentic identity is an Entra Agent ID (`microsoft.graph.agentIdentity`) and is
  the suffix of Copilot `AgentId` (`T_<title>.<agentic id>`).
- The agent identities sign in to Bot Framework (`AADServicePrincipalSignInLogs`) once
  per Copilot interaction, without a user. They never call Graph themselves.
- TraceBack `last_hop.actions[].requester` now walks Graph `ClientRequestId` → MCP audit
  → agentic ID → CopilotInteraction users (−15 min/+1 min). Live result: app-only 17:52
  action is `sole_candidate` (admin); delegated 17:56 action `agrees_with_api_user`.
  17:14/17:17 actions predate schema v3 and report `no_agent_identity`.
- Still a lead, not proof: one self-reported link, agent-scoped. ANALYST-PIVOT 3b has
  the KQL (verified live).
- Note: Graph logs `RequestUri` with a raw `@`, not `%40`.

### 2026-10-11 rule: Microsoft platform logs only (supersedes the requester bridge above)

The agent-identity bridge depended on our MCP server writing the Foundry baggage header
to its own console log. That is custom logging that another MCP server would not have,
so it was removed. TraceBack now never queries `ContainerAppConsoleLogs` (plan
`native=False`). Re-verified live: delegated `user_identified`/`token_exact`; app-only
`workload_identified` for our managed identity, human `not_in_api_telemetry`.

Standard logs checked for an app-only link to the user (17:52Z run), all negative:
- Graph activity / managed identity sign-in: workload only.
- Agent identity Bot Framework sign-ins: no user, no conversation.
- Admin `M365 Agent Connector` / `Enterprise Copilot Platform` sign-ins: user's own Copilot
  tokens; nothing shared with Foundry or MCP.
- Foundry `AzureDiagnostics` RequestResponse: `callerObjectId` empty, no tool detail.
- `ContainerAppHTTPLogs`: user agent `AzureAIFoundryAgentRuntime` only (also host-specific).

Last out-of-box option under test: Foundry tracing. Connection `tb-appi-tracing` (App
Insights `tb-appi-rig` → Sentinel workspace) added 2026-10-11 ~18:45Z. Needs a retest.

### 2026-10-12 retest with Foundry tracing on (app-only 20:41Z, delegated 20:44Z)

TraceBack (native only): delegated `user_identified`/`token_exact`; app-only `workload_identified`/`token_exact`, human not in API telemetry.

Foundry tracing (out-of-box: App Insights connected to the project) writes `AppDependencies`
`invoke_agent` / `execute_tool <server>.get_user_by_upn` spans and `AppGenAIContent`, with
`gen_ai.conversation.id` (`conv_…`), trace ID, tool call ID, tool arguments
(`{"upn":"user1@contoso.com"}`), the full tool result (user1's object ID),
`microsoft.channel.name = msteams:COPILOT` and `microsoft.a365.agent.blueprint.id`. It records **no user**.

A search of every table for each ID found three separate groups that share no IDs:
1. CopilotActivity: user, Copilot conversation GUID, Teams thread, message ID, agent identity.
2. Foundry tracing: `conv_…`, trace, tool call. The blueprint ID matches the agent identity in
   Entra (`agentIdentityBlueprintId`), which is a directory lookup, not a log join.
3. Graph: the managed identity call. Its ClientRequestId appears only in our own MCP console log.

The app-only chain therefore has two links with no shared ID:
- Graph ↔ Foundry tool span: target, time (~5 s) and the result object ID.
- Foundry agent ↔ Copilot prompt: agent identity/blueprint and time (~26 s).

It is a strong, corroborated lead but not proof. Delegated/OBO is the only exact path.

## Requester lead implemented (Application Insights + Entra + Copilot audit)

`last_hop.requester_lead` (see README) runs one bounded query over
`AppDependencies`, `AADServicePrincipalSignInLogs` and `CopilotActivity`. It
requires Application Insights connected to the Foundry project. It is a lead,
never proof, and never changes the verdict.

Live results against the 2026-10-12 runs:
- App-only: `sole_candidate` = admin, prompt 27 s before the tool span; the
  verdict stays `workload_identified`.
- Delegated: same method names the same user the token proves
  (`agrees_with_token: true`), which validates the lead chain.
- Actions before Application Insights had tool spans report `no_tool_span`.

Remaining weak links (no Microsoft log carries a shared ID):
Graph ↔ tool span (target + time) and Copilot prompt ↔ tool span (agent + time).

