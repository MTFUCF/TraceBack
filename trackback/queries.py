"""Fixed, bounded read-only KQL plans. Input is encoded scalar data only."""

import base64
import json
import os
import re
from dataclasses import dataclass
from types import MappingProxyType

from .models import InvestigationRequest, TIMESPANS, safe_id

MAX_ROWS = 100
MAX_KEYS = 128
# MatchRank tiers: per-event IDs, then target, then session/trace context, then broad actor/app.
RANK_EVENT, RANK_TARGET, RANK_CONTEXT, RANK_BROAD = 0, 1, 2, 3
SEED_MAX_RANK = RANK_TARGET
MAX_KEY_BYTES = 4096
_TABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)


def encode_scalar(value):
    return "base64_decode_tostring('" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "')"


def column(name):
    return f'tostring(column_ifexists("{name}", ""))'


def copilot_field(name):
    root = 'column_ifexists("LLMEventData", dynamic({}))'
    return f"coalesce({root}.{name}, {root}.CopilotEventData.{name})"


def copilot_messages():
    messages = copilot_field("Messages")
    extracted = ", ".join(f"tostring({messages}[{i}].Id)" for i in range(16))
    return (
        f"array_concat(coalesce(array_slice({copilot_field('MessageIds')}, 0, 15), dynamic([])), "
        f"pack_array({extracted}))"
    )


COMMON = {
    "TimeGenerated": "TimeGenerated",
    "RequestId": column("RequestId"),
    "ClientRequestId": column("ClientRequestId"),
    "CorrelationId": column("CorrelationId"),
    "OperationId": column("OperationId"),
    "SessionId": column("SessionId"),
    "UserId": column("UserId"),
    "AppId": column("AppId"),
    "ServicePrincipalId": column("ServicePrincipalId"),
}
APP_INSIGHTS = {
    **COMMON, "OperationId": column("OperationId"),
    "ParentId": column("ParentId"), "Id": column("Id"),
    "TraceId": 'coalesce(tostring(column_ifexists("Properties", dynamic({}))["trace_id"]), '
    + column("OperationId") + ')',
    "SpanId": 'coalesce(tostring(column_ifexists("Properties", dynamic({}))["span_id"]), '
    + column("Id") + ')',
    "AgentId": 'tostring(column_ifexists("Properties", dynamic({}))["gen_ai.agent.id"])',
    "ConversationId": 'tostring(column_ifexists("Properties", dynamic({}))["gen_ai.conversation.id"])',
    "OperationName": column("OperationName"),
    "Name": column("Name"),
    "Target": column("Target"),
    "DependencyType": column("DependencyType"),
    "ResultCode": column("ResultCode"),
    "AppRoleName": column("AppRoleName"),
    "ResourceId": column("_ResourceId"),
}
SIGNIN = {
    **COMMON, "OriginalRequestId": column("OriginalRequestId"),
    "UniqueTokenIdentifier": column("UniqueTokenIdentifier"),
    "ResourceServicePrincipalId": column("ResourceServicePrincipalId"),
    "ResultType": column("ResultType"),
}
SERVICE = {
    **COMMON, "OperationId": column("OperationId"),
    "ParentId": column("ParentId"), "Id": column("Id"),
    "TraceId": 'tostring(column_ifexists("Properties", dynamic({}))["trace_id"])',
    "SpanId": 'tostring(column_ifexists("Properties", dynamic({}))["span_id"])',
    "AgentId": 'tostring(column_ifexists("Properties", dynamic({}))["gen_ai.agent.id"])',
    "ConversationId": 'tostring(column_ifexists("Properties", dynamic({}))["gen_ai.conversation.id"])',
    "OperationName": column("OperationName"),
    "Name": column("Name"),
    "ResourceId": column("_ResourceId"),
}
AZURE_DIAGNOSTICS = {
    **COMMON,
    "OperationId": 'coalesce(' + column("OperationId") + ', ' + column("operationId_s") + ')',
    "CorrelationId": 'coalesce(' + column("CorrelationId") + ', ' + column("correlationId_s") + ')',
    "RequestId": 'coalesce(' + column("RequestId") + ', ' + column("requestId_s") + ')',
    "Category": 'coalesce(' + column("Category") + ', ' + column("category_s") + ')',
    "OperationName": 'coalesce(' + column("OperationName") + ', ' + column("operationName_s") + ')',
    "ResourceProvider": 'coalesce(' + column("ResourceProvider") + ', ' + column("ResourceProvider_s") + ')',
    "ResourceId": 'coalesce(' + column("_ResourceId") + ', ' + column("ResourceId") + ')',
}
MCP_JSON = 'parse_json(coalesce(' + column("Log_s") + ', ' + column("Log") + ', ' + column("Message") + '))'
MCP_PLATFORM = f"coalesce({MCP_JSON}.platform_ids, dynamic({{}}))"
MCP_TOKEN = f"coalesce({MCP_JSON}.token_claims, dynamic({{}}))"
MCP_OUTBOUND = f"coalesce({MCP_JSON}.outbound_token_claims, dynamic({{}}))"
MCP_CONSOLE = {
    "TimeGenerated": "TimeGenerated",
    "RequestId": f"tostring({MCP_JSON}.graph_request_id)",
    "ClientRequestId": f"tostring({MCP_JSON}.graph_client_request_id)",
    "CorrelationId": f"coalesce(tostring({MCP_PLATFORM}['x-ms-correlation-id']), tostring({MCP_PLATFORM}['x-correlation-id']))",
    "OperationId": f"coalesce(tostring({MCP_PLATFORM}['traceparent']), tostring({MCP_PLATFORM}['x-ms-request-id']))",
    "SessionId": f"coalesce(tostring({MCP_PLATFORM}['x-ms-foundry-thread-id']), tostring({MCP_PLATFORM}['thread-id']))",
    "UserId": f"coalesce(tostring({MCP_TOKEN}.oid), tostring({MCP_TOKEN}.sub))",
    "AppId": f"coalesce(tostring({MCP_TOKEN}.appid), tostring({MCP_TOKEN}.azp))",
    "TraceId": f"tostring(split(tostring({MCP_PLATFORM}['traceparent']), '-')[1])",
    "InboundTokenId": f"tostring({MCP_TOKEN}.uti)",
    "OutboundTokenId": f"tostring({MCP_OUTBOUND}.uti)",
    "ToolName": f"tostring({MCP_JSON}.tool_name)",
    "AuthMode": f"tostring({MCP_JSON}.auth_mode)",
    "Event": f"tostring({MCP_JSON}.event)",
    "GraphRequestUri": 'substring(' + f"tostring({MCP_JSON}.graph_request_uri)" + ', 0, 4096)',
    "HttpStatus": f"tostring({MCP_JSON}.http_status)",
    "PlatformThreadId": f"tostring({MCP_PLATFORM}['x-ms-foundry-thread-id'])",
    "PlatformRunId": f"tostring({MCP_PLATFORM}['x-ms-foundry-run-id'])",
    "ResourceId": column("_ResourceId"),
}
CONTAINER_HTTP = {
    "TimeGenerated": "coalesce(column_ifexists('StartTime', datetime(null)), TimeGenerated)",
    "RequestId": column("RequestId"),
    "ClientRequestId": column("ClientRequestId"),
    "CorrelationId": column("CorrelationId"),
    "OperationId": column("OperationId"),
    "SessionId": column("SessionId"),
    "ContainerAppName": column("ContainerAppName"),
    "RevisionName": column("RevisionName"),
    "Method": column("Method"),
    "Path": 'substring(' + column("Path") + ', 0, 1024)',
    "StatusCode": column("StatusCode"),
    "RequestDuration": column("RequestDuration"),
    "ResourceId": column("_ResourceId"),
}


@dataclass(frozen=True)
class QueryPlan:
    source: str
    hops: tuple[str, ...]
    projection: tuple[tuple[str, str], ...]
    search_columns: tuple[str, ...]
    required: bool = True
    table_env: str | None = None
    # False for logs an application writes about itself. Live investigations use
    # Microsoft platform logs only, so these are never queried.
    native: bool = True


def _env_name(source):
    return "TRACKBACK_TABLE_" + re.sub(r"[^A-Za-z0-9]+", "_", source).strip("_").upper()


def configured_table_name(plan_or_source):
    source = plan_or_source.source if isinstance(plan_or_source, QueryPlan) else plan_or_source
    selected = QUERY_PLANS[source] if not isinstance(plan_or_source, QueryPlan) else plan_or_source
    value = os.getenv(selected.table_env or _env_name(source), source)
    if not isinstance(value, str) or _TABLE_NAME.fullmatch(value) is None:
        raise ValueError("invalid_table_configuration")
    return value


def plan(source, hops, fields, search=(), required=True, table_env=None, native=True):
    return QueryPlan(source, hops, tuple(fields.items()), tuple(search), required, table_env or _env_name(source), native)


def app_plan(source, fields):
    suffix = re.sub(r"(?<!^)([A-Z])", r"_\1", source).upper()
    search = ("Name", "Target") if source == "AppDependencies" else ()
    return plan(
        source, ("H3", "H4"), fields, search=search,
        table_env=f"TRACKBACK_TABLE_{suffix}",
    )


_plans = (
    plan("MicrosoftGraphActivityLogs", ("H6", "H7"), {
        **COMMON, "SignInActivityId": column("SignInActivityId"),
        "Scopes": 'substring(' + column("Scopes") + ', 0, 2048)',
        "Roles": 'substring(' + column("Roles") + ', 0, 2048)',
        "RequestMethod": column("RequestMethod"),
        "ResponseStatusCode": column("ResponseStatusCode"),
        "RequestUri": 'substring(' + column("RequestUri") + ', 0, 4096)',
    }, ("RequestUri",)),
    plan("AADServicePrincipalSignInLogs", ("H5",), SIGNIN),
    plan("AADManagedIdentitySignInLogs", ("H5",), SIGNIN),
    plan("AADNonInteractiveUserSignInLogs", ("H1", "H5"), SIGNIN),
    plan("SigninLogs", ("H1", "H5"), SIGNIN),
    plan("CopilotActivity", ("H1", "H2"), {
        **COMMON, "RecordId": column("RecordId"),
        "ActorUserId": column("ActorUserId"), "ActorUserType": column("ActorUserType"),
        "AgentId": column("AgentId"), "AppIdentity": column("AppIdentity"),
        "ConversationId": f"tostring({copilot_field('ConversationId')})",
        "ThreadId": f"tostring({copilot_field('ThreadId')})",
        "MessageIds": copilot_messages(),
        "TargetPlatformAgentId": f"tostring({copilot_field('TargetPlatformAgentId')})",
        "TargetTaskIds": f"array_slice({copilot_field('TargetTaskIds')}, 0, 15)",
    }),
    *(app_plan(source, APP_INSIGHTS) for source in (
        "AppRequests", "AppDependencies", "AppTraces", "AppEvents", "AppExceptions"
    )),
    plan("AzureDiagnostics", ("H3",), AZURE_DIAGNOSTICS, required=False, table_env="TRACKBACK_TABLE_AZURE_DIAGNOSTICS"),
    plan(
        "ContainerAppConsoleLogs", ("H4",), MCP_CONSOLE,
        ("Log_s", "Log", "Message"), required=False,
        table_env="TRACKBACK_TABLE_CONTAINER_APP_CONSOLE_LOGS", native=False,
    ),
    plan("ContainerAppHTTPLogs", ("H4",), CONTAINER_HTTP, ("ContainerAppName", "Path"), required=False, table_env="TRACKBACK_TABLE_CONTAINER_APP_HTTP_LOGS"),
    plan("AuditLogs", ("H7",), {
        **COMMON, "Id": column("Id"), "OperationName": column("OperationName"),
        "TargetId": 'tostring(column_ifexists("TargetResources", dynamic([]))[0].id)',
        "TargetType": 'tostring(column_ifexists("TargetResources", dynamic([]))[0].type)',
    }, ("TargetResources",), required=False),
    plan("AzureActivity", ("H7",), {
        **COMMON, "OperationName": column("OperationNameValue"),
        "ResourceId": column("ResourceId"), "ActivityStatusValue": column("ActivityStatusValue"),
    }, ("ResourceId",), required=False),
    plan("SecurityAlert", ("H7",), {
        **COMMON, "Id": column("SystemAlertId"),
    }, ("Entities",), required=False),
    plan("SecurityIncident", ("H7",), {
        **COMMON, "Id": column("IncidentName"),
    }, ("Title",), required=False),
)
_BACKWARD_ORDER = (
    "MicrosoftGraphActivityLogs", "AADServicePrincipalSignInLogs",
    "AADManagedIdentitySignInLogs", "AADNonInteractiveUserSignInLogs",
    "ContainerAppConsoleLogs", "AppDependencies", "ContainerAppHTTPLogs",
    "AppRequests", "AppTraces", "AppEvents", "AzureDiagnostics",
    "CopilotActivity", "SigninLogs", "AppExceptions", "AuditLogs", "AzureActivity",
    "SecurityAlert", "SecurityIncident",
)
QUERY_PLANS = MappingProxyType({name: next(p for p in _plans if p.source == name) for name in _BACKWARD_ORDER})


def bound_keys(keys):
    """Ordered input keeps caller priority; unordered input is sorted for determinism."""
    ordered = keys if isinstance(keys, (list, tuple)) else sorted(keys)
    identifiers = list(dict.fromkeys(v for value in ordered if (v := safe_id(value)) is not None))
    selected = []
    for identifier in identifiers:
        if len(selected) == MAX_KEYS or len(json.dumps([*selected, identifier]).encode("utf-8")) > MAX_KEY_BYTES:
            return tuple(selected), True
        selected.append(identifier)
    return tuple(selected), False


def build_query(plan_id, target, timespan="P30D", keys=()):
    request = InvestigationRequest(target, "delegated_permissions", timespan)
    selected = QUERY_PLANS[plan_id]
    identifiers, _ = bound_keys(keys)
    header = (
        f"let _target = {encode_scalar(request.target)};\n"
        f"let _keys = todynamic({encode_scalar(json.dumps(identifiers, separators=(',', ':')))});\n"
        # Graph and MCP URIs carry the percent-encoded UPN (%40) or the object ID.
        "let _target_enc = url_encode_component(_target);\n"
        "let _target_oid = toscalar(union isfuzzy=true (datatable(AccountObjectId:string)[]), "
        "(IdentityInfo | where TimeGenerated >= ago(14d) and AccountUPN =~ _target "
        "| project AccountObjectId = tostring(AccountObjectId)) "
        "| where isnotempty(AccountObjectId) | summarize take_any(AccountObjectId));\n"
    )
    # Fixed scalar and typed-array expressions also cover nested platform IDs.
    id_columns = {
        "UserId", "ActorUserId", "AppId", "AppIdentity", "AgentId",
        "ServicePrincipalId", "RequestId", "ClientRequestId", "CorrelationId",
        "SessionId", "UniqueTokenIdentifier", "OriginalRequestId", "SignInActivityId",
        "OperationId", "Id",
    }
    predicates = [f"{column(name)} in (_keys)" for name in sorted(id_columns)]
    # Rank decides which rows survive the row limit and which may seed later keys:
    # a per-event ID (token, request) beats the target, which beats a shared
    # session/trace, which beats rows that only share a broad actor/app identity.
    broad = {"UserId", "ActorUserId", "AppId", "AppIdentity", "AgentId", "ServicePrincipalId"}
    context = {"SessionId", "CorrelationId", "OperationId", "ConversationId", "ThreadId",
               "TargetPlatformAgentId", "TraceId"}
    event = [f"{column(name)} in (_keys)" for name in sorted(id_columns - broad - context)]
    shared = [f"{column(name)} in (_keys)" for name in sorted(id_columns & context)]
    target = []
    nested_ids = {
        "ConversationId", "ThreadId", "TargetPlatformAgentId", "TraceId", "SpanId", "AgentId",
        "InboundTokenId", "OutboundTokenId",
    }
    for name, expression in selected.projection:
        if name in id_columns or name in nested_ids:
            predicates.append(f"({expression}) in (_keys)")
            if name in context:
                shared.append(f"({expression}) in (_keys)")
            elif name not in broad:
                event.append(f"({expression}) in (_keys)")
        if name in {"MessageIds", "TargetTaskIds"}:
            predicates.append(f"array_length(set_intersect({expression}, _keys)) > 0")
            event.append(f"array_length(set_intersect({expression}, _keys)) > 0")
    for name in selected.search_columns:
        target_match = (
            f"{column(name)} contains _target",
            f"{column(name)} contains _target_enc",
            f"(isnotempty(_target_oid) and {column(name)} contains _target_oid)",
        )
        predicates.extend(target_match)
        target.extend(target_match)
    tiers = [(event, RANK_EVENT), (target, RANK_TARGET), (shared, RANK_CONTEXT)]
    rank = ", ".join(
        "(" + " or ".join(f"({p})" for p in preds) + f"), {value}" for preds, value in tiers if preds
    )
    projection = ",\n    ".join(f"{name} = {expression}" for name, expression in selected.projection)
    bounded = ", ".join(
        f'{name} = iff(strlen({name}) <= 512, {name}, "")'
        for name, _ in selected.projection
        if name not in {"TimeGenerated", "MessageIds", "TargetTaskIds", "RequestUri", "Scopes", "Roles"}
    )
    invalid_ids = " or ".join(
        f"strlen({name}) > 512" for name, _ in selected.projection
        if name not in {"TimeGenerated", "MessageIds", "TargetTaskIds", "RequestUri", "Scopes", "Roles"}
    )
    return (
        header + configured_table_name(selected) + f"\n| where TimeGenerated >= ago({TIMESPANS[timespan]}) and TimeGenerated <= now()\n"
        "| where " + " or ".join(f"({p})" for p in predicates) + "\n"
        f"| extend MatchRank = case({rank}, {RANK_BROAD})\n"
        "| project MatchRank, " + projection + "\n"
        "| extend InvalidIdentifier = " + invalid_ids + "\n"
        "| extend " + bounded + "\n"
        "| extend _order = tostring(pack_array(" + ", ".join(name for name, _ in selected.projection) + "))\n"
        "| order by MatchRank asc, TimeGenerated asc, _order asc\n"
        "| project-away _order\n"
        f"| take {MAX_ROWS + 1}"
    )
