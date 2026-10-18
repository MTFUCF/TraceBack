# Security and Privacy Requirements

## Security posture

TraceBack is a local-only, read-only investigation proof of concept. It is not
approved for public, shared, multi-user, or production deployment.

The application may read telemetry through an existing Azure CLI session, but
it must not modify Azure resources, identities, diagnostic settings, incidents,
or telemetry. Missing telemetry is an evidence gap, not proof that attribution
is impossible.

## Threat model

### Protected assets

- Azure CLI authentication context and tenant/subscription metadata
- User, service principal, managed identity, and agent identifiers
- Prompts, messages, URLs, IP addresses, request bodies, claims, and dynamic
  telemetry properties
- Correlation, request, session, conversation, trace, span, and token IDs
- Provenance envelopes, signatures, nonces, and identity hashes
- Investigation inputs, results, and query failures

### Trust boundaries

1. Browser to the local Flask process
2. Flask routes to the telemetry adapter
3. Telemetry adapter to the Azure CLI
4. Azure CLI to Log Analytics/Sentinel
5. Raw telemetry to normalized evidence
6. Normalized evidence to browser rendering

Inputs and telemetry values are untrusted at every boundary. In particular,
telemetry may contain attacker-controlled strings capable of KQL injection,
HTML injection, log injection, or misleading attribution.

### Security goals

- Keep credentials and raw telemetry inside the adapter boundary.
- Make every cloud operation read-only, bounded, and attributable to a fixed
  query plan.
- Prevent user-controlled table names, KQL operators, time expressions, or
  projection lists.
- Never select an identity or edge merely because it is the first or most
  frequent result.
- Distinguish a successful query with no matches from unavailable data,
  authorization failure, and query failure.
- Render only encoded, normalized evidence in the browser.

## Local-only deployment restrictions

- Bind only to `127.0.0.1` or `::1`. Binding to `0.0.0.0` is prohibited.
- Flask debug mode and the interactive debugger must be disabled.
- Do not place the app behind a public ingress, reverse proxy, tunnel, shared
  development endpoint, or remotely accessible container.
- Reject requests whose `Host` or `Origin` is not an explicit loopback
  allowlist.
- Use a dedicated Azure identity with only the read permissions required to
  query the selected workspace and inspect resource metadata.
- Pass the subscription and workspace explicitly on every Azure CLI call. Do
  not depend on or change the caller's default subscription.
- Permit only read commands. Resource writes, diagnostic-setting changes,
  incident updates, role assignments, deployments, and data ingestion are out
  of scope.
- Set subprocess timeouts and concurrency limits. Invoke the CLI with an
  argument array and without a shell.
- Do not persist investigation results, raw CLI output, browser history, or
  telemetry caches. Temporary data must remain in memory and be discarded
  after the response.

The process must fail closed when these restrictions cannot be enforced.

## Privacy boundary

Raw Azure CLI results must be consumed only by the telemetry adapter. Route
handlers, templates, browser JavaScript, application logs, and error responses
must never receive raw rows or raw dynamic property bags.

The adapter may return only an allowlisted `EvidenceRecord` representation:

- `source`: allowlisted logical source name
- `hop`: one of the seven defined hops
- `event_time`: normalized UTC timestamp
- `evidence_type`: allowlisted evidence category
- `actor_type`: `user`, `service_principal`, `managed_identity`, `agent`, or
  `unknown`
- `action_category` and `resource_type`: normalized enums
- `correlation_key_type`: allowlisted key type
- `correlation_ref`: response-local opaque label such as `corr-01`
- `outcome`, `confidence`, and allowlisted `reason_codes`

The response must exclude names, UPNs, email addresses, IP addresses, URLs,
request bodies, prompts, messages, claims, tokens, credential metadata, raw
identifiers, raw hashes, signatures, nonces, `RawData`, `Properties`,
`LLMEventData`, `envelope_json_s`, and arbitrary source columns. Raw identifiers
may be compared inside the adapter, but the browser receives only response-local
opaque references. Do not use unsalted hashes of low-entropy identifiers as a
privacy control.

The health endpoint may report boolean readiness and a non-sensitive workspace
label. It must not return an `az account show` object, tenant identity, signed-in
user details, access tokens, subscription inventory, or CLI stderr.

Application logging must contain query-plan IDs, durations, row counts, and
sanitized outcome codes only. It must not contain investigation terms, raw KQL
values, rows, identities, correlation IDs, or unsanitized Azure CLI errors.

## KQL safety

- Queries must be selected by immutable query-plan ID from a code allowlist.
- Table names, joins, projections, operators, and limits must never come from a
  request.
- Time ranges must be selected from a small allowlist such as `P7D`, `P30D`,
  and `P90D`; arbitrary timespan strings are prohibited.
- Search values must be normalized, length-bounded, and encoded as scalar data
  by one reviewed helper. Direct interpolation of request values into KQL
  source is prohibited.
- Every data query must include a time bound and a deterministic row cap.
- Project only fields needed by the adapter; do not use `search *`, wildcard
  table unions, or return complete records.
- Schema discovery must use management-plane table metadata, `getschema`, the
  `Usage` table, or aggregate field-presence queries.
- Query plans must be read-only. Kusto control commands and ingestion or update
  operations are prohibited.
- A failure in one source must remain visible as a failure or partial result;
  it must not be converted into an empty successful result.

## Query outcomes

Every query produces exactly one explicit `QueryOutcome`:

| State | Meaning |
|---|---|
| `observed` | Query succeeded and produced one or more normalized evidence records. |
| `no_matches` | Query succeeded and produced no matching rows in the requested window. |
| `source_unavailable` | The table or required schema is not queryable in the workspace. |
| `access_denied` | Azure authentication succeeded but the query was not authorized. |
| `query_failed` | The service, CLI, parser, timeout, or query failed. |
| `invalid_request` | Input failed local validation and no query was executed. |

An investigation may additionally be `partial` when its component queries have
different outcomes. `no_matches`, `source_unavailable`, `access_denied`,
`query_failed`, and `partial` must never be presented as impossible
attribution.

## Attribution model

The result contains exactly seven ordered hops and six independently assessed
links:

1. Original human requester
2. Microsoft 365 Copilot or originating agent
3. Foundry agent
4. MCP-hosting service
5. Workload identity or token subject
6. Microsoft Graph or other API action
7. Affected target

Each link must have its own deterministic `EdgeAssessment` containing the
source hop, destination hop, query outcomes, exact key types used, bounded time
relationship, confidence, and reason codes.

Correlation precedence is:

1. Exact request, operation, token, provenance, trace, or span ID
2. Exact signed provenance claim that is valid and unexpired
3. Exact session/conversation ID with uniqueness checks and a bounded time
   window
4. Exact actor/application ID plus a bounded time window, marked as a candidate
   link only

Timestamp proximity, shared application identity, shared session ID with
multiple candidates, row order, and record counts are not deterministic proof.
Ambiguous matches must remain ambiguous. The implementation must not select the
first row as the answer or derive confidence from the number of returned rows.

## Delegated and application attribution

Delegated and application permissions are separate attribution paths:

- **Delegated:** a user or delegated token signal may support the human-to-action
  chain. User ID, scopes, and the matching sign-in/token event must agree.
- **Application:** service principal or managed identity evidence attributes the
  action to the workload identity only. The original human requires a separate,
  explicit upstream link such as signed provenance or a deterministic agent
  request correlation.
- **Mixed or unknown:** contradictory, incomplete, or missing actor signals must
  not be coerced into either mode.

Application ownership, resource ownership, the most recent interactive user,
temporal proximity alone, or a shared app ID must never be reported as the
originating human.

## Browser and API safety

- Render all values as text. Do not insert telemetry or request values with
  `innerHTML`.
- Return a fixed JSON schema with size limits and no raw query-result section.
- Use a restrictive Content Security Policy and avoid inline script where
  practical.
- Reject oversized JSON bodies, unknown fields, unsupported modes, and invalid
  time ranges.
- Apply loopback `Host`/`Origin` validation and request-rate limits to prevent a
  malicious local web page from driving expensive queries.
- Return generic client errors while recording only sanitized internal outcome
  codes.

## Acceptance criteria

The POC is acceptable only when all of the following are true:

1. The server binds to loopback only and debug mode is disabled.
2. Azure access is read-only and every CLI call has a timeout, explicit
   subscription, and explicit workspace.
3. The health response exposes no Azure account object or private identity.
4. Every query is selected from a fixed plan, uses an allowlisted time range,
   and returns only bounded projected fields.
5. Injection tests cover quotes, backslashes, KQL operators, oversized values,
   malformed JSON, and unsupported time ranges.
6. The adapter returns only allowlisted `EvidenceRecord` values and never raw
   telemetry, arbitrary dynamic bags, credentials, or raw CLI errors.
7. All query paths produce an explicit `QueryOutcome`; partial failure remains
   visible.
8. Results always contain seven hops and six link assessments in deterministic
   order.
9. Exact-key joins are distinguished from ambiguous temporal candidates; ties
   are not resolved by row order.
10. Delegated, application, mixed, and unknown attribution semantics are tested
    independently.
11. Missing data is reported as an evidence gap, never as impossible
    attribution.
12. Browser rendering treats every returned value as untrusted text and passes
    stored-XSS tests using hostile telemetry fixtures.
13. No raw telemetry, identities, secrets, tokens, account objects, or query
    values are written to logs or persistent files.
14. Startup fails closed if configured for non-loopback access, debug mode, an
    unapproved workspace, or a non-read-only command plan.

## Current review disposition

The current POC is **not approved for deployment**. The reviewed implementation
binds to all interfaces with debug mode enabled, returns the Azure account
object from its health endpoint, builds KQL from request values, suppresses
query failures into success-shaped results, returns raw telemetry rows, and
renders telemetry with `innerHTML`. These conditions must be remediated and the
acceptance criteria above must pass before the app is used outside an isolated
developer workstation.
