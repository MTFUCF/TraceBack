# Microsoft Graph Evidence Sources

**Assessment date:** September 7, 2026

## Conclusion

Microsoft Graph does **not** expose a Microsoft Graph REST resource that returns
the same per-request records as the Azure Monitor
`MicrosoftGraphActivityLogs` table.

The full request envelope used by TraceBack is available only after Microsoft
Graph activity-log collection is enabled and routed through an Azure Monitor
diagnostic setting. Supported destinations are:

- A Log Analytics workspace. Microsoft Sentinel does not have to be enabled on
  that workspace for the logs to be queried.
- An Azure Storage account.
- An Event Hubs namespace, normally with a downstream consumer or archive.

If the category wasn't enabled and routed when an event occurred, Microsoft
doesn't retain a recoverable Microsoft Graph activity-log copy. Collection
isn't retroactive.

TraceBack should support alternative APIs as **degraded corroboration
providers**, but it must not treat them as substitutes for
`MicrosoftGraphActivityLogs`. Without the Graph activity-log request envelope,
TraceBack can't prove the exact Graph request, its permission mode, or the
native token-to-request link required for a complete H5-to-H6 attribution.

The operating default should therefore be **API first and ingestion off**.
TraceBack should query existing Entra and preprovisioned Purview APIs before
requesting any Graph activity-log collection. If full H6 proof is necessary
and no retained Graph activity log already exists, collection may be enabled
only for an explicitly approved test. The verified enabled interval must not
exceed one hour.

## What TraceBack currently requires

The current telemetry implementation treats `MicrosoftGraphActivityLogs` as a
required source for H6 and H7. Its normalizer uses:

- `RequestId`, `ClientRequestId`, and `OperationId` as request correlation keys.
- `SignInActivityId` as the token/sign-in correlation key.
- `AppId`, `ServicePrincipalId`, and `UserId` for actor classification.
- `Scopes` versus `Roles` to distinguish delegated and application
  permissions.
- `RequestUri` to identify the target resource.
- `RequestMethod` to classify the action.
- `ResponseStatusCode` to classify success or failure.

The H5-to-H6 design expects:

```text
MicrosoftGraphActivityLogs.SignInActivityId
    =
sign-in telemetry.UniqueTokenIdentifier
```

The existing adapter queries a Log Analytics workspace with Azure CLI. It
doesn't call a Microsoft Sentinel-specific API. Therefore, a workspace that
contains `MicrosoftGraphActivityLogs` remains usable even when Microsoft
Sentinel isn't enabled on that workspace.

## Comparison matrix

| Source | Access path | Event granularity and useful fields | Permissions and licensing | Retention and latency | TraceBack evidentiary value |
|---|---|---|---|---|---|
| **Microsoft Graph activity logs** | Azure Monitor diagnostic setting to Log Analytics, Storage, or Event Hubs | One record per Microsoft Graph HTTP request. Contract includes `RequestId`, `ClientRequestId`, `OperationId`, `SignInActivityId`, `AppId`, `ServicePrincipalId`, `UserId`, `Scopes`, `Roles`, `RequestUri`, `RequestMethod`, and `ResponseStatusCode`. | Microsoft Entra ID P1 or P2. Security Administrator is the least-privileged role documented for configuring the diagnostic setting. Runtime access is granted at the destination, for example Log Analytics Reader or narrower table-level access. | Microsoft Entra retains no copy unless the category is integrated with a destination. Retention is destination-controlled. Microsoft documents delivery within 30 minutes in most regions and up to two hours in less common cases. | **Authoritative H6 source.** Can establish the exact Graph request, result, target, app/user identity, delegated/application permission mode, and the native sign-in link. |
| **Entra sign-ins API** | Microsoft Graph `GET /auditLogs/signIns` | Authentication events. v1.0 exposes sign-in ID, correlation ID, app/user/resource identifiers, sign-in type details, and status. Current Graph metadata shows `servicePrincipalId` in v1.0. It does not expose the Graph HTTP request URI, method, status, scopes, roles, or Graph request ID. | `AuditLog.Read.All`; delegated callers also need a supported Entra role such as Reports Reader or Security Reader. Microsoft documents P1 or P2 for downloading sign-in logs through Graph. | Available only inside Entra's default window: seven days for Free and 30 days for P1/P2. Microsoft documents latency factors rather than an event-level SLA. First-time external integrations can take 24 hours, and initial Log Analytics routing can take up to three days. | **Useful H5 corroboration, not H6.** v1.0 doesn't expose `uniqueTokenIdentifier`, so it can't reproduce TraceBack's exact `SignInActivityId` join. Beta metadata currently exposes `uniqueTokenIdentifier` and `originalRequestId`, but beta isn't production-supported and still doesn't provide the Graph request envelope. |
| **Entra directory audit API** | Microsoft Graph `GET /auditLogs/directoryAudits` | Directory changes with time, operation, result, `correlationId`, initiating user/app, and target resources. It records the audited directory operation, not every HTTP request that reached Graph. | `AuditLog.Read.All` is least privileged; `Directory.Read.All` is broader. Delegated callers need a supported role such as Reports Reader, Security Reader, or Security Administrator. | Seven days with Entra Free and 30 days with P1/P2. No fixed event-availability SLA is published on the API page. | **Useful H7 effect/target corroboration.** It can support who or what changed a directory object, but doesn't prove `RequestId`, `ClientRequestId`, `SignInActivityId`, scopes/roles, exact request URI/method, or the Graph response status. |
| **Purview unified audit log through Audit Search Graph API** | Create a search with `POST /security/auditLog/queries`, then read query state and records with GET | Cross-service audit events with operation, service, actor/user type, object ID, result fields, and workload-specific `auditData.dynamicProperties`. Field presence varies by service. This is the unified audit log, not Microsoft Graph request logging. | Service-level permissions such as `AuditLogsQuery-Entra.Read.All` or workload-specific permissions; `AuditLogsQuery.Read.All` is the broad option. Audit (Standard) or Audit (Premium) licensing and Purview access controls apply. | Audit (Standard) retains records for 180 days. Audit (Premium) retains Entra, Exchange, OneDrive, and SharePoint records for one year by default for appropriately licensed users; a separate add-on supports up to 10 years. Core-service records are typically available after 60-90 minutes, with no guaranteed time; broad searches can take up to 48 hours. | **Useful H1/H2/H7 or workload corroboration, not equivalent H6.** Some workload-specific data may contain correlation fields, but there is no common contract for the complete Graph request envelope. Starting a search creates a persistent query job and conflicts with TraceBack's current no-cloud-mutation rule. |
| **Purview unified audit log through Office 365 Management Activity API** | Preconfigure an activity-feed subscription, then use GET operations to list subscriptions, list content, and retrieve content blobs | Same unified audit ecosystem, grouped into `Audit.AzureActiveDirectory`, `Audit.Exchange`, `Audit.SharePoint`, `Audit.General`, and DLP feeds. Common fields include audit record ID, creation time, operation, workload, user ID/type, object ID, and result, plus workload-specific fields. | App registration, tenant-admin consent, and `ActivityFeed.Read`; unified audit logging must be enabled. Audit (Standard) or Audit (Premium) licensing applies. | A new subscription can take up to 12 hours to produce its first blobs. Content-list queries can start no more than seven days in the past, and returned blobs have an expiration. Backend unified-audit retention remains license/policy controlled. Event order and delivery latency aren't guaranteed. | **Potential GET-only provider only after external provisioning.** It can corroborate audited operations and effects but not supply the stable Graph request envelope. TraceBack must not start or change subscriptions itself under the current security policy. |
| **Microsoft Graph reports endpoints** | Microsoft Graph `/reports/*`; Graph API usage is currently a beta report | Usage and adoption reports. The beta Graph API usage report is daily aggregate data with date, service area, tenant, app, usage count, and active-app count. It has no per-request records. | Most usage reports use `Reports.Read.All`; delegated users also need an allowed reporting role. The beta Graph API usage report supports delegated access only and requires Global Reader or Global Administrator. Availability depends on the relevant Microsoft 365 subscription. | General usage reports commonly cover 7, 30, 90, or 180 days. The beta Graph API usage report covers 7, 30, or 60 days. Microsoft 365 usage reports typically appear within 24-72 hours and can take several days. | **Not forensic request evidence.** It can establish aggregate app usage trends but can't prove any individual call, actor, target, permission, URI, method, status, or correlation chain. |
| **Azure Activity Log** | Azure Monitor Activity Log portal, CLI, PowerShell, or REST API | Azure Resource Manager control-plane events such as create, update, delete, and action operations on Azure resources. It doesn't record Microsoft Graph directory or Microsoft 365 data-plane calls. | Azure RBAC read access at the relevant scope. Reader or Monitoring Reader are typical built-in choices; a custom role must include the activity-log read action. No Microsoft 365 or Entra premium license is required. | Azure retains events for 90 days. Events are generally available in 3-20 minutes. Diagnostic settings can export them for longer retention. | **Context only.** It can prove deployment, diagnostic-setting, role-assignment, or other ARM activity, but can't prove a Graph data-plane action. |
| **Service-specific audit logs** | Purview unified audit log, service admin portals, and workload-specific APIs where available | Workload operations such as Exchange mailbox access, SharePoint/OneDrive file activity, Teams actions, or service administration. Schemas, identifiers, and coverage differ by workload. | Depends on the service. When accessed through Purview or the Management Activity API, the permissions and licensing above apply. Separate service APIs have their own permissions and licensing. | Depends on the service; Purview-backed records follow Purview retention and latency. | **Target-side corroboration only unless a documented exact key is present.** A business operation can be confirmed, but it must not be promoted to the original Graph HTTP request without an exact, documented cross-source identifier. |

## Does Microsoft Graph expose the same per-request data?

No.

Verified findings:

1. Microsoft's Graph activity-log documentation exposes the full per-request
   schema only through Azure Monitor diagnostic settings and downstream
   destinations.
2. The Microsoft Graph v1.0 and beta service metadata available on
   September 7, 2026 contains no entity set named
   `MicrosoftGraphActivityLogs` and no Graph-activity-log entity set.
3. A direct candidate
   `/auditLogs/microsoftGraphActivityLogs` request isn't supported.
4. Entra `signIn`, Entra `directoryAudit`, Purview `auditLogRecord`, and Graph
   reports are different resources with different collection semantics and
   schemas.

Field-level comparison:

| Required TraceBack field | Graph activity logs | Entra sign-ins API | Entra directory audits | Purview/Management Activity | Graph reports | Azure Activity Log |
|---|---:|---:|---:|---:|---:|---:|
| `RequestId` for the Graph HTTP request | Yes | No | No | Not a common guarantee | No | ARM request only |
| `ClientRequestId` | Yes | No | No | Workload-specific at best | No | ARM request only |
| `SignInActivityId` | Yes | No | No | No common guarantee | No | No |
| `UniqueTokenIdentifier` counterpart | Sign-in join target | Beta sign-in metadata only; not v1.0 | No | No common guarantee | No | No |
| `AppId` | Yes | Yes | Initiator app where applicable | Workload-specific | Aggregate app ID in some reports | ARM claims where applicable |
| `ServicePrincipalId` | Yes | Yes | Initiator app, but different shape | Workload-specific | No request-level value | ARM claims where applicable |
| `UserId` | Yes | Yes | Initiator user | Common actor field, semantics vary | Some reports, often aggregated | ARM caller/claims |
| OAuth `Scopes` / app `Roles` | Yes | No | No | No common guarantee | No | ARM claims, not Graph token permissions |
| `RequestUri` | Yes | No | No | Object/workload fields, not a stable Graph URI contract | No | ARM resource URI only |
| HTTP method | Yes | No | Operation type, not HTTP method | Operation name, not guaranteed HTTP method | No | ARM method may be present |
| Graph response status | Yes | Sign-in result only | Audit operation result | Workload audit result | No | ARM operation status only |

The beta `signIn` schema deserves one narrow qualification: the service
metadata currently includes `uniqueTokenIdentifier` and `originalRequestId`.
That can strengthen H5 evidence and can join to a separately available Graph
activity record. It does not independently establish that a Graph request
occurred, and beta APIs aren't supported for production use.

## Read-only probe results

All probes were run on September 7, 2026 with the existing Azure CLI session.
Every `az rest` operation used `GET`. Responses were reduced locally to counts,
capability booleans, or sanitized outcome codes. No identities, telemetry
request URIs, destination identifiers, tokens, or record contents were
retained in this document.

| Probe | Result | Interpretation |
|---|---|---|
| Azure CLI session | Authenticated and enabled | Read-only probes were possible. |
| Entra diagnostic settings list | Success; 3 settings visible | Current identity can enumerate tenant diagnostic settings. |
| `MicrosoftGraphActivityLogs` diagnostic category | Enabled in 2 settings; both route to Log Analytics; 0 Storage and 0 Event Hubs destinations | Graph request telemetry is currently configured for Log Analytics. Whether either workspace has Sentinel enabled wasn't inspected and isn't required for Azure Monitor queries. |
| Entra directory audits, previous hour, capped at one | Success; 1 record present | Current identity can read `/auditLogs/directoryAudits`. No record contents were displayed. |
| Entra sign-ins, previous hour, capped at one | Success; 1 record present | Current identity can read `/auditLogs/signIns`. No record contents were displayed. |
| Purview Audit Search query metadata | Access denied | Current CLI identity/token lacks the required Audit Search Graph permission or service access. No query was created. |
| Office 365 Management Activity subscriptions | Access denied | Current CLI identity/token lacks `ActivityFeed.Read` or equivalent consent. No subscription was created or changed. |
| Microsoft Graph API usage report, beta | Access denied | Current CLI identity/token lacks the report permission and/or required role. |
| Azure Activity Log, previous hour | Success; 0 records in the narrow window | API access works; no matching recent control-plane event was returned. |
| Microsoft Graph v1.0 metadata | No Graph activity-log entity set; `signIn` lacks `uniqueTokenIdentifier` and `originalRequestId` | The stable API can't reproduce the token join or request envelope. |
| Microsoft Graph beta metadata | No Graph activity-log entity set; `signIn` includes `uniqueTokenIdentifier` and `originalRequestId` | Beta can improve sign-in correlation only; it still isn't a Graph request log and isn't production-supported. |

The current workspace's table contents and retention configuration weren't
queried because that would require selecting a destination workspace and
issuing a Log Analytics data query. The diagnostic-setting result establishes
that Log Analytics routing is configured, not that a particular event is still
retained.

The diagnostic-setting state was checked again on September 7, 2026 at
approximately 18:16 EDT (22:16 UTC), after the one-hour constraint was
supplied. Two settings still reported
`MicrosoftGraphActivityLogs.enabled = true`, both with Log Analytics
destinations. No change was made. Because their enable times aren't exposed by
the diagnostic-setting objects, the current state doesn't prove compliance
with a one-hour test-only window. An authorized owner must review and disable
those category entries if no approved test is active.

## API-first operating policy

Use the following evidence order:

1. Query Microsoft Graph v1.0 `auditLogs/signIns` and
   `auditLogs/directoryAudits` with fixed projections, narrow UTC windows, and
   strict result caps.
2. Query an already provisioned Purview or Office 365 Management Activity feed
   if one exists and TraceBack has approved read-only access.
3. Query existing service-specific audit APIs only for the affected workload.
4. Return `partial` or `unresolved` when these sources establish authentication
   or an audited effect but no Graph request envelope exists.
5. Request temporary `MicrosoftGraphActivityLogs` ingestion only when the test
   objective explicitly requires exact H6 proof: request IDs, Graph URI and
   method, response status, scopes/roles, and the native sign-in link.

API-first operation reduces ingestion cost and avoids changing tenant
diagnostic configuration. It doesn't change the evidentiary standard: absence
of Graph activity logs remains an evidence gap, not evidence that an Entra or
Purview event was caused by a particular Graph request.

## Recommended architecture

### 1. Use an API-first corroboration layer

Add the bounded, GET-only Entra provider described below and optionally consume
an already provisioned Management Activity feed. Run these providers before
checking for Graph activity logs. If they answer the investigation's limited
question, don't enable ingestion.

The API-first layer must make its reduced capability visible. It may report
authentication and audited-effect evidence, but it must not silently promote
that evidence to H6 or to complete human attribution.

### 2. Preserve Graph activity logs as the only authoritative H6 provider

Keep `MicrosoftGraphActivityLogs` as mandatory for a complete attribution.
Generalize its storage backend instead of redefining weaker audit events as
Graph requests:

- Keep the existing Log Analytics provider. Rename documentation and
  configuration language from "Sentinel" to "Log Analytics/Azure Monitor"
  where appropriate, because Sentinel enablement isn't required.
- Add a read-only Azure Storage provider for previously archived
  `MicrosoftGraphActivityLogs` blobs if customers use Storage instead of Log
  Analytics.
- Support Event Hubs only through an approved downstream archive or consumer
  store. Event Hubs alone isn't a durable historical investigation source.

Each backend should normalize the same Microsoft Graph activity-log schema so
the H6/H7 and H5-to-H6 semantics remain identical.

Do not leave ingestion continuously enabled for TraceBack. Prefer existing
retained records. When a new capture is indispensable, use the bounded workflow
in the next section.

### 3. Add an Entra API corroboration provider

A bounded, GET-only provider for:

- `GET /auditLogs/signIns`
- `GET /auditLogs/directoryAudits`

is reasonable when Log Analytics sign-in or directory-audit tables aren't
available. It should use fixed `$select` lists, fixed time-window filters,
bounded paging, explicit tenant validation, no arbitrary OData from the
browser, and the existing privacy-preserving normalization boundary.

This provider may emit:

- H5 sign-in evidence.
- H7 directory-change or target-effect evidence.

It must **not** emit an H6 `api_action` record and must not mark H5-to-H6 as
linked without an actual Graph activity record carrying the corresponding
native identifier.

Use Microsoft Graph v1.0 by default. Beta `uniqueTokenIdentifier` support may
be evaluated behind an explicit experimental flag, but it must not be required
for production or used to claim a Graph request when H6 is absent.

### 4. Treat Purview as optional workload corroboration

Two possible integrations have different security consequences:

- **Audit Search Graph API:** useful for targeted unified-audit searches, but
  TraceBack would have to create a search job with POST before reading its
  records. That violates the current "no cloud mutations" rule. Don't add this
  provider unless the security boundary is explicitly revised and approved.
- **Office 365 Management Activity API:** GET-only ingestion is possible after
  an administrator has already granted consent and created the activity-feed
  subscriptions outside TraceBack. TraceBack must never start, stop, or alter a
  subscription.

Purview records should normalize as agent, audit, or target corroboration based
on the documented workload schema. They must not satisfy the H6 request
requirement merely because they describe the same business operation.

### 5. Do not add Graph reports as an evidence provider

Reports are delayed aggregate usage data. They are suitable for capacity,
adoption, or anomaly baselines, not request attribution.

### 6. Keep Azure Activity Log explicitly control-plane-only

Retain Azure Activity Log as optional context for Azure resource changes. Never
use it to assert that a Microsoft Graph data-plane request occurred.

### 7. Make source capability explicit

Any future provider contract should declare capabilities rather than relying on
source-name assumptions:

| Capability | Meaning |
|---|---|
| `graph_request_envelope` | Has the exact Graph request ID, URI, method, and response status. |
| `graph_permission_mode` | Has the token scopes/roles needed to distinguish delegated and application access. |
| `graph_signin_link` | Has the native Graph-to-sign-in correlation identifier. |
| `actor_authentication` | Has a sign-in record for a user, service principal, or managed identity. |
| `audited_effect` | Has a service or directory audit record for the affected target. |
| `aggregate_only` | Contains trends or counts, not event evidence. |

An investigation can be `complete` only when the selected H6 provider has the
first three capabilities. Entra and Purview fallbacks should produce an
explicit `partial` result with a reason such as
`graph_request_evidence_unavailable`; they shouldn't increase confidence by
combining app identity and timestamp proximity.

## One-hour Microsoft Graph activity-log capture workflow

This workflow is for an authorized tenant administrator. TraceBack must never
execute the enable, update, or delete operations itself.

There is no native one-hour time-to-live setting for an Entra diagnostic
setting. The one-hour control must be enforced operationally and measured from
verified configuration state. Azure Monitor's GET response proves the desired
configuration, but Microsoft doesn't publish a guarantee that the underlying
collection pipeline starts or stops at that exact instant.

### Preconditions

All of these conditions are mandatory:

- The API-first sources were queried and the test owner documented why exact
  H6 evidence is still required.
- A named Security Administrator, test operator, stop authority, and backup
  stop operator are available for the complete window.
- The exact destination and its retention/cost controls are approved.
- The operator has permission to use the destination. The TraceBack runtime has
  read-only access only.
- An independent timer or automation owned by the administrator is set to
  initiate shutdown no later than 50 minutes after verified enablement. This
  leaves ten minutes to verify the disabled state.
- All existing diagnostic settings have been inventoried with a tenant-scope
  GET and their complete configuration saved in an approved restricted
  location outside this repository.
- The baseline GET shows zero enabled `MicrosoftGraphActivityLogs` category
  entries. If it doesn't, the test is **NO-GO** because the one-hour boundary
  can't be established.

The current read-only probe found two enabled category entries, so the final
precondition isn't currently demonstrated. The user must have an authorized
Security Administrator review them and disable them, while preserving all
unrelated log categories and destinations, before using this workflow.

### Control-plane operations

For Microsoft Entra tenant diagnostics, use the
`Microsoft.AADIAM/diagnosticSettings` tenant resource. The relevant operation
shapes are:

```text
GET    /providers/microsoft.aadiam/diagnosticSettings
PUT    /providers/microsoft.aadiam/diagnosticSettings/{setting-name}
DELETE /providers/microsoft.aadiam/diagnosticSettings/{setting-name}
```

Use an approved API version, such as the documented
`2017-04-01`/`2017-04-01-preview` versions for the tenant resource. Use a
dedicated temporary diagnostic setting when quota and policy permit. This
avoids replacing or damaging unrelated categories in an existing setting.

If an existing setting must be changed, first GET and preserve its complete
representation. A create-or-update operation uses PUT, so an incomplete body
can remove unrelated configuration. Restore the exact baseline body at stop.

### Timed procedure

1. **Baseline at `T_baseline`.** GET all Entra diagnostic settings. Verify and
   record a sanitized count of zero enabled `MicrosoftGraphActivityLogs`
   entries. Confirm the destination is queryable and the test target and UTC
   window are approved.
2. **Request enablement at `T_enable_request`.** The authorized administrator,
   outside TraceBack, creates the dedicated setting or updates the approved
   setting with only the intended category change.
3. **Verify enablement at `T_enable_verified`.** Immediately GET all settings
   until exactly one approved setting reports
   `MicrosoftGraphActivityLogs.enabled = true` and the expected destination
   type. Don't expose the setting name or destination ID in TraceBack output.
   The 60-minute clock starts at the timestamp of this successful GET, not when
   the change was requested.
4. **Run the test once.** Execute only the preapproved controlled action.
   Record its UTC start/end times and approved target identifier in the
   restricted operator worksheet. Don't retry within the same window.
5. **Request shutdown by minute 50.** The administrator deletes the dedicated
   temporary setting, or restores the complete baseline configuration with
   `MicrosoftGraphActivityLogs` disabled. Do not wait for the evidence record
   to arrive before disabling collection.
6. **Verify shutdown immediately.** GET all settings and calculate the count of
   entries where the category is `MicrosoftGraphActivityLogs` and `enabled` is
   true. The required result is zero.
7. **Verify shutdown again five minutes later.** Repeat the GET and require the
   same zero count. Define `T_disable_verified` as this second successful
   verification. Require:

   ```text
   T_disable_verified - T_enable_verified <= 60 minutes
   ```

   If the count isn't zero or the interval exceeds 60 minutes, declare the
   capture-control test failed and notify the cost/security owner.
8. **Query after disablement.** Search the destination at approximately 30
   minutes and two hours after the test. Use the event-time window
   `T_enable_verified` through `T_disable_verified`, a fixed target filter,
   fixed projections, and a strict row cap. Late delivery of in-window events
   is expected and doesn't mean the setting is still enabled.
9. **Handle first-time routing conservatively.** Microsoft documents that Graph
   activity events normally reach the destination within 30 minutes and can
   take up to two hours. Separately, a newly configured Entra-to-Log Analytics
   integration can take up to three days to begin showing data. If this is a
   first-time destination and the row is absent after two hours, keep the
   result inconclusive and check again within the documented setup-latency
   window. Don't re-enable the category or extend the capture beyond one hour.
10. **Close the test.** Confirm the category remains disabled, retain only the
    sanitized evidence required by policy, and calculate ingestion/retention
    cost from the destination. Disabling collection doesn't delete records
    already delivered or queued for delivery.

### Verification semantics and caveats

- The one-hour requirement is enforceable as a **verified configuration
  interval**. Microsoft doesn't document a hard event-pipeline cutoff at the
  exact GET verification timestamp.
- Records can arrive after disablement because delivery is asynchronous. Judge
  inclusion by event time, not query time or ingestion arrival time.
- An event whose `TimeGenerated` falls after `T_disable_verified` requires
  investigation, but a late-arriving event with an in-window `TimeGenerated`
  doesn't by itself show that ingestion remained enabled.
- If the business requirement means no bytes may arrive after the 60-minute
  mark, Azure Monitor's documented asynchronous delivery can't provide that
  guarantee. In that case, do not enable the category.
- A missing event after a one-hour capture is an evidence gap. It isn't proof
  that no Graph request occurred.

## What an alternate API provider can and cannot prove

An Entra/Purview alternate provider **can** support:

- That a user, service principal, or managed identity authenticated.
- That a directory or Microsoft 365 workload recorded an operation.
- That a target object was changed or accessed when the workload emitted a
  suitable audit event.
- That actor, app, object, result, and time values are consistent across
  sources.
- An exact link when two sources independently expose the same documented
  platform-generated identifier.

It **cannot**, without `MicrosoftGraphActivityLogs`, prove:

- The exact Microsoft Graph HTTP request that caused the effect.
- The Graph `RequestId`, `ClientRequestId`, or `SignInActivityId`.
- The request's OAuth delegated scopes or application roles.
- The exact Graph URI and HTTP method.
- The Graph response status for that request.
- That the authenticated principal observed in a nearby sign-in issued the
  audited operation.
- The original human behind an application-permission call without a separate,
  exact upstream chain.

## Security considerations

- Don't query or log raw identities, IP addresses, request URIs, claims,
  `auditData.dynamicProperties`, or unrelated tenant records.
- Keep all server-side API filters immutable except for validated scalar time
  and target values.
- Apply strict page, row, byte, timeout, and concurrency limits.
- Store raw identifiers only in request-local memory and expose only
  response-local opaque references.
- Don't use the Azure CLI's general-purpose account object as an API response
  or health payload.
- Don't enable diagnostic settings, create Purview search jobs, start
  Management Activity subscriptions, grant consent, or change retention from
  the TraceBack runtime.
- Don't use Graph beta endpoints by default. If beta sign-in fields are tested,
  isolate them behind a capability check and fail closed when absent.
- Treat missing data, access denied, unsupported schema, expired retention, and
  provider failure as different outcomes.
- Treat workload-specific dynamic audit properties as untrusted and
  privacy-sensitive.

## Required user or administrator action

No additional user input is required for this documentation result.

Before implementing or enabling an alternate provider, the coordinator needs
these explicit decisions:

1. **Current-state correction:** an authorized Security Administrator must
   review the two currently enabled `MicrosoftGraphActivityLogs` diagnostic
   settings. If no approved test is active, disable the category in both while
   preserving unrelated categories and destinations. TraceBack must not make
   this change.
2. **API-first approval:** confirm that preliminary investigations should stop
   at `partial` or `unresolved` when the GET-only Entra/Purview sources provide
   corroboration but no H6 request envelope.
3. **Destination choice:** approve the specific Log Analytics workspace or
   Storage archive that contains `MicrosoftGraphActivityLogs`, and grant the
   TraceBack runtime read-only access. Supply resource identifiers through the
   existing secure configuration path, not in source control.
4. **One-hour capture authorization:** for each test, approve the test owner,
   exact UTC window, destination, target, Security Administrator, stop
   authority, backup stop operator, and the requirement that shutdown starts
   by minute 50 and is verified twice by minute 60.
5. **Entra API provider:** grant a dedicated TraceBack application
   `AuditLog.Read.All`, confirm the applicable Entra license, and assign only
   the least-privileged supported role for delegated operation. The existing
   developer CLI access isn't an acceptable production credential.
6. **Purview provider:** decide whether to keep TraceBack strictly GET-only.
   If yes, an administrator must provision the Management Activity API
   subscription outside TraceBack and grant `ActivityFeed.Read`. If the Audit
   Search Graph API is desired, explicitly approve its POST-based query-job
   creation as a change to the current no-cloud-mutation policy and grant only
   the required `AuditLogsQuery-*.Read.All` permission.
7. **Retention:** confirm the actual destination and Purview retention settings.
   License defaults and diagnostic routing don't prove that the required event
   is still available.

## Authoritative Microsoft references

- Microsoft Graph activity logs overview:
  https://learn.microsoft.com/en-us/graph/microsoft-graph-activity-logs-overview
- `MicrosoftGraphActivityLogs` Azure Monitor table:
  https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/microsoftgraphactivitylogs
- Microsoft Entra retention:
  https://learn.microsoft.com/en-us/entra/identity/monitoring-health/reference-reports-data-retention
- Microsoft Entra log latency:
  https://learn.microsoft.com/en-us/entra/identity/monitoring-health/reference-log-latency
- List sign-ins:
  https://learn.microsoft.com/en-us/graph/api/signin-list?view=graph-rest-1.0
- Sign-in resource, v1.0:
  https://learn.microsoft.com/en-us/graph/api/resources/signin?view=graph-rest-1.0
- Sign-in resource, beta:
  https://learn.microsoft.com/en-us/graph/api/resources/signin?view=graph-rest-beta
- List directory audits:
  https://learn.microsoft.com/en-us/graph/api/directoryaudit-list?view=graph-rest-1.0
- Directory-audit resource:
  https://learn.microsoft.com/en-us/graph/api/resources/directoryaudit?view=graph-rest-1.0
- Purview auditing overview:
  https://learn.microsoft.com/en-us/purview/audit-solutions-overview
- Purview audit search:
  https://learn.microsoft.com/en-us/purview/audit-search
- Purview audit retention policies:
  https://learn.microsoft.com/en-us/purview/audit-log-retention-policies
- Create an Audit Search Graph query:
  https://learn.microsoft.com/en-us/graph/api/security-auditcoreroot-post-auditlogqueries?view=graph-rest-1.0
- List Audit Search Graph query records:
  https://learn.microsoft.com/en-us/graph/api/security-auditlogquery-list-records?view=graph-rest-1.0
- Audit Search Graph record schema:
  https://learn.microsoft.com/en-us/graph/api/resources/security-auditlogrecord?view=graph-rest-1.0
- Office 365 Management Activity API:
  https://learn.microsoft.com/en-us/office/office-365-management-api/office-365-management-activity-api-reference
- Office 365 Management Activity API setup:
  https://learn.microsoft.com/en-us/office/office-365-management-api/get-started-with-office-365-management-apis
- Office 365 Management Activity API schemas:
  https://learn.microsoft.com/en-us/office/office-365-management-api/office-365-management-activity-api-schema
- Microsoft Graph reports overview:
  https://learn.microsoft.com/en-us/graph/reportroot-concept-overview
- Microsoft Graph API usage report, beta:
  https://learn.microsoft.com/en-us/graph/api/reportroot-getapiusage?view=graph-rest-beta
- Microsoft 365 usage-report availability:
  https://learn.microsoft.com/en-us/microsoft-365/admin/activity-reports/activity-reports
- Azure Activity Log:
  https://learn.microsoft.com/en-us/azure/azure-monitor/platform/activity-log
- Azure Activity Log REST API:
  https://learn.microsoft.com/en-us/rest/api/monitor/activity-logs/list?view=rest-monitor-2015-04-01
- Azure Monitor ingestion latency:
  https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-ingestion-time
- Log Analytics access control:
  https://learn.microsoft.com/en-us/azure/azure-monitor/logs/manage-access
- Integrate Entra logs with Azure Monitor:
  https://learn.microsoft.com/en-us/entra/identity/monitoring-health/howto-integrate-activity-logs-with-azure-monitor-logs
- Microsoft Entra diagnostic-settings resource:
  https://learn.microsoft.com/en-us/azure/templates/microsoft.aadiam/diagnosticsettings
- List diagnostic settings:
  https://learn.microsoft.com/en-us/rest/api/monitor/diagnostic-settings/list?view=rest-monitor-2021-05-01-preview
- Create or update diagnostic settings:
  https://learn.microsoft.com/en-us/rest/api/monitor/diagnostic-settings/create-or-update?view=rest-monitor-2021-05-01-preview
- Delete diagnostic settings:
  https://learn.microsoft.com/en-us/rest/api/monitor/diagnostic-settings/delete?view=rest-monitor-2021-05-01-preview
