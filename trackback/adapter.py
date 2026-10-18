"""Azure boundary: bounded CLI execution, schema normalization and offline fixtures."""

from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit

from .models import EvidenceRecord, InvestigationRequest, QueryOutcome, safe_id, utc_timestamp
from .queries import MAX_ROWS, QUERY_PLANS, RANK_CONTEXT, SEED_MAX_RANK, bound_keys, build_query
from .requester_lead import LeadRows, build_lead_query

MAX_OUTPUT_BYTES = 2 * 1024 * 1024
QUERY_TIMEOUT = 20
INVESTIGATION_TIMEOUT = 90
_GUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_RESOURCE_GROUP = r"[A-Za-z0-9_.()-]{0,89}[A-Za-z0-9_()-]"
_WORKSPACE_NAME = r"[A-Za-z0-9][A-Za-z0-9-]{2,61}[A-Za-z0-9]"
_RESOURCE = re.compile(
    rf"/subscriptions/({_GUID})/resourceGroups/({_RESOURCE_GROUP})"
    rf"/providers/Microsoft\.OperationalInsights/workspaces/({_WORKSPACE_NAME})",
    re.IGNORECASE | re.ASCII,
)
_CONFIGURATION_MESSAGES = {
    "missing_workspace_identity": (
        "Set LOG_ANALYTICS_WORKSPACE_RESOURCE_ID or all of AZURE_SUBSCRIPTION_ID, "
        "LOG_ANALYTICS_RESOURCE_GROUP and LOG_ANALYTICS_WORKSPACE_NAME."
    ),
    "invalid_workspace_resource_id": (
        "LOG_ANALYTICS_WORKSPACE_RESOURCE_ID must be a full Log Analytics workspace "
        "resource ID with a GUID subscription and valid resource group and workspace names."
    ),
    "invalid_subscription_id": "AZURE_SUBSCRIPTION_ID must be a GUID.",
    "invalid_resource_group": (
        "LOG_ANALYTICS_RESOURCE_GROUP must contain 1-90 ASCII letters, digits, "
        "underscores, hyphens, periods or parentheses, and cannot end in a period."
    ),
    "invalid_workspace_name": (
        "LOG_ANALYTICS_WORKSPACE_NAME must contain 4-63 ASCII letters, digits or "
        "hyphens, starting and ending with a letter or digit."
    ),
    "invalid_customer_id": "LOG_ANALYTICS_WORKSPACE_ID must be a GUID.",
    "invalid_tenant_id": "AZURE_TENANT_ID must be a GUID.",
    "workspace_identity_mismatch": (
        "AZURE_SUBSCRIPTION_ID, LOG_ANALYTICS_RESOURCE_GROUP and "
        "LOG_ANALYTICS_WORKSPACE_NAME must match the corresponding segments of "
        "LOG_ANALYTICS_WORKSPACE_RESOURCE_ID whenever supplied."
    ),
    "tenant_mismatch": (
        "AZURE_TENANT_ID does not match the Azure CLI account tenant for the "
        "configured subscription."
    ),
}


class WorkspaceConfigurationError(ValueError):
    """Only fixed diagnostics, never configured values or Azure account details."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(_CONFIGURATION_MESSAGES[reason])

    def public(self):
        return {
            "error": "invalid_workspace_configuration",
            "reason": self.reason,
            "message": str(self),
        }


def _validate_setting(value, pattern, reason):
    if not isinstance(value, str) or re.fullmatch(pattern, value, re.ASCII) is None:
        raise WorkspaceConfigurationError(reason)


@dataclass(frozen=True)
class WorkspaceConfig:
    resource_id: str
    customer_id: str | None = None
    tenant_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.resource_id, str) or not _RESOURCE.fullmatch(self.resource_id):
            raise WorkspaceConfigurationError("invalid_workspace_resource_id")
        if self.customer_id is not None:
            _validate_setting(self.customer_id, _GUID, "invalid_customer_id")
        if self.tenant_id is not None:
            _validate_setting(self.tenant_id, _GUID, "invalid_tenant_id")

    @property
    def subscription_id(self):
        return _RESOURCE.fullmatch(self.resource_id).group(1)

    @classmethod
    def from_environment(cls):
        resource = os.getenv("LOG_ANALYTICS_WORKSPACE_RESOURCE_ID")
        customer = os.getenv("LOG_ANALYTICS_WORKSPACE_ID")
        tenant = os.getenv("AZURE_TENANT_ID")
        subscription = os.getenv("AZURE_SUBSCRIPTION_ID")
        workspace_name = os.getenv("LOG_ANALYTICS_WORKSPACE_NAME")
        resource_group = os.getenv("LOG_ANALYTICS_RESOURCE_GROUP")
        components = (subscription, resource_group, workspace_name)
        for value, pattern, reason in (
            (subscription, _GUID, "invalid_subscription_id"),
            (resource_group, _RESOURCE_GROUP, "invalid_resource_group"),
            (workspace_name, _WORKSPACE_NAME, "invalid_workspace_name"),
            (customer, _GUID, "invalid_customer_id"),
            (tenant, _GUID, "invalid_tenant_id"),
        ):
            if value is not None:
                _validate_setting(value, pattern, reason)
        if resource is None:
            if any(value is None for value in components):
                raise WorkspaceConfigurationError("missing_workspace_identity")
            resource = (
                f"/subscriptions/{subscription}/resourceGroups/{resource_group}/"
                f"providers/Microsoft.OperationalInsights/workspaces/{workspace_name}"
            )
        config = cls(resource, customer, tenant)
        # Compare the complete identity, including any partially supplied form.
        if any(
            value is not None and value.casefold() != segment.casefold()
            for value, segment in zip(components, _RESOURCE.fullmatch(resource).groups())
        ):
            raise WorkspaceConfigurationError("workspace_identity_mismatch")
        return config


class OutputLimitExceeded(RuntimeError):
    pass


def bounded_runner(args, *, timeout, max_output_bytes=MAX_OUTPUT_BYTES, **kwargs):
    """Drain both pipes concurrently and kill the child at the byte/time boundary."""
    if kwargs.get("shell"):
        raise ValueError("shell_not_allowed")
    if not math.isfinite(timeout) or timeout <= 0 or not 0 < max_output_bytes <= MAX_OUTPUT_BYTES:
        raise ValueError("invalid_execution_bounds")
    environment = dict(os.environ)
    environment.update({
        "AZURE_CORE_COLLECT_TELEMETRY": "no",
        "AZURE_LOGGING_ENABLE_LOG_FILE": "no",
        "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no",
        "AZURE_CORE_ONLY_SHOW_ERRORS": "true",
    })
    chunks = [bytearray(), bytearray()]
    exceeded = threading.Event()
    with subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=False, env=environment,
    ) as process:
        def drain(pipe, destination):
            while block := pipe.read1(4096):
                if len(destination) + len(block) > max_output_bytes:
                    exceeded.set()
                    break
                destination.extend(block)

        threads = [
            threading.Thread(target=drain, args=(pipe, chunks[i]), daemon=True)
            for i, pipe in enumerate((process.stdout, process.stderr))
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if exceeded.is_set():
                    raise OutputLimitExceeded("output_limit")
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("azure_query", timeout)
                time.sleep(0.02)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for thread in threads:
                thread.join()
        if exceeded.is_set():
            raise OutputLimitExceeded("output_limit")
        return subprocess.CompletedProcess(
            args, process.returncode,
            bytes(chunks[0]).decode("utf-8"), bytes(chunks[1]).decode("utf-8"),
        )


def cli_command():
    executable = shutil.which("az") or shutil.which("az.cmd")
    if not executable:
        return None
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        # Never pass query text through cmd.exe's metacharacter parser.
        for python in (Path(executable).parent.parent / "python.exe", Path(executable).parent / "python.exe"):
            if python.is_file():
                return [str(python), "-I", "-m", "azure.cli"]
        return None
    return [executable]


_ID_COLUMNS = {
    "RequestId": "request_id", "OriginalRequestId": "original_request_id",
    "ClientRequestId": "client_request_id", "OperationId": "operation_id",
    "CorrelationId": "correlation_id", "SessionId": "session_id",
    "UniqueTokenIdentifier": "token_id", "SignInActivityId": "token_id",
    "TraceId": "trace_id", "SpanId": "span_id", "ParentId": "parent_span_id",
    "UserId": "user_id", "ActorUserId": "user_id", "AppId": "app_id",
    "AppIdentity": "app_id", "ServicePrincipalId": "service_principal_id",
    "ResourceServicePrincipalId": "resource_service_principal_id",
    "AgentId": "agent_id", "TargetPlatformAgentId": "target_agent_id",
    "ConversationId": "conversation_id", "ThreadId": "thread_id",
    "MessageId": "message_id", "MessageIds": "message_id",
    "TargetTaskIds": "task_id", "TargetId": "target_id", "ResourceId": "resource_id",
    "PlatformThreadId": "thread_id", "PlatformRunId": "task_id",
    "InboundTokenId": "token_id", "OutboundTokenId": "token_id",
}


def _dynamic(value, expected):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            return expected()
    return value if isinstance(value, expected) else expected()


def _identifiers(source, row):
    ids = {}
    nested = _dynamic(row.get("LLMEventData"), dict) if source == "CopilotActivity" else {}
    nested = {**_dynamic(nested.get("CopilotEventData"), dict), **nested}
    properties = _dynamic(row.get("Properties"), dict) if source.startswith("App") or source == "AzureDiagnostics" else {}
    selected = dict(row)
    if source == "AuditLogs":
        targets = _dynamic(row.get("TargetResources"), list)
        target_ids = [target.get("id") for target in targets[:16] if isinstance(target, dict)]
        if target_ids and not selected.get("TargetId"):
            selected["TargetId"] = target_ids
    for name in ("ConversationId", "ThreadId", "MessageIds", "TargetPlatformAgentId", "TargetTaskIds"):
        if name not in selected and name in nested:
            selected[name] = nested[name]
    messages = nested.get("Messages", [])
    if isinstance(messages, list):
        selected["MessageIds"] = [
            *(_dynamic(selected.get("MessageIds"), list)),
            *(item.get("Id") for item in messages[:16] if isinstance(item, dict)),
        ][:32]
    for key, name in (
        ("trace_id", "TraceId"), ("span_id", "SpanId"),
        ("gen_ai.agent.id", "AgentId"), ("gen_ai.conversation.id", "ConversationId"),
    ):
        if not selected.get(name) and properties.get(key):
            selected[name] = properties[key]
    if source.startswith("App") and not selected.get("TraceId"):
        selected["TraceId"] = selected.get("OperationId")
    for column, kind in _ID_COLUMNS.items():
        values = selected.get(column, ())
        if isinstance(values, str) and values.startswith("["):
            values = _dynamic(values, list)
        if isinstance(values, str):
            values = (values,)
        if not isinstance(values, (tuple, list)):
            if values:
                raise ValueError("invalid_record")
            continue
        for value in values[:32]:
            if value is None or value == "":
                continue
            identifier = safe_id(value)
            if identifier is None:
                raise ValueError("invalid_record")
            ids.setdefault(kind, set()).add(identifier)
    event_id = safe_id(row.get("RecordId") or row.get("Id"))
    if event_id and len(event_id) <= 450:
        ids["source_event_id"] = {source + ":" + event_id}
    if source in {"AppRequests", "AppDependencies"} and safe_id(row.get("Id")):
        ids["span_id"] = {safe_id(row["Id"])}
    return {kind: tuple(sorted(values)) for kind, values in ids.items()}


def _http_outcome(value):
    code = str(value or "")
    return "succeeded" if code.isdigit() and 200 <= int(code) < 400 else "failed" if code.isdigit() else "unknown"


def _graph_target(uri):
    if not isinstance(uri, str) or len(uri) > 4096:
        return "unknown", None
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError:
        return "unknown", None
    if parsed.netloc and not parsed.scheme or port not in {None, 443}:
        return "unknown", None
    if parsed.scheme and (
        parsed.scheme != "https" or parsed.username or parsed.password
        or parsed.hostname not in {"graph.microsoft.com", "graph.microsoft.us", "dod-graph.microsoft.us"}
    ):
        return "unknown", None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parts and parts[0] in {"v1.0", "beta"}:
        parts = parts[1:]
    resources = {
        "users": "user", "groups": "group", "applications": "application",
        "servicePrincipals": "service_principal", "sites": "site",
        "drives": "file", "messages": "message",
    }
    if len(parts) < 2 or parts[0] not in resources or parts[1].startswith(("$", "microsoft.graph.")):
        return "unknown", None
    return resources[parts[0]], safe_id(parts[1])


def normalize_row(source, row):
    """Raw source values are consumed here, never copied to public evidence."""
    if source not in QUERY_PLANS or not isinstance(row, dict):
        raise ValueError("invalid_record")
    invalid_identifier = row.get("InvalidIdentifier")
    if invalid_identifier not in {None, False, 0, "", "false", "False"}:
        raise ValueError("invalid_record")
    event_time = utc_timestamp(row.get("TimeGenerated") or row.get("Timestamp") or row.get("event_time"))
    ids = _identifiers(source, row)
    base = dict(source=source, event_time=event_time, ids=ids)
    if source == "MicrosoftGraphActivityLogs":
        scopes = row.get("Scopes")
        roles = row.get("Roles")
        has_scopes = bool(scopes) and scopes not in ("[]", "{}", "null")
        has_roles = bool(roles) and roles not in ("[]", "{}", "null")
        permission = "mixed" if has_scopes and has_roles else (
            "delegated_permissions" if has_scopes else "application_permissions" if has_roles else "unknown"
        )
        actor = "user" if permission == "delegated_permissions" and ids.get("user_id") else (
            "service_principal" if permission == "application_permissions" else "unknown"
        )
        if permission == "application_permissions" and ids.get("user_id"):
            permission, actor = "mixed", "unknown"
        resource, target = _graph_target(row.get("RequestUri"))
        if target:
            ids = {**ids, "target_id": (target,)}
            base["ids"] = ids
        method = row.get("RequestMethod", "")
        action = {"GET": "read", "POST": "execute", "PUT": "update", "PATCH": "update", "DELETE": "delete"}.get(method, "unknown")
        code = str(row.get("ResponseStatusCode", ""))
        outcome = _http_outcome(code)
        record = EvidenceRecord(
            **base, hop="H6", evidence_type="api_action", actor_type=actor,
            permission_mode=permission, resource_type=resource,
            action_category=action, outcome=outcome,
        )
        return (record, replace(record, hop="H7", evidence_type="target")) if target else (record,)
    if source in {"SigninLogs", "AADNonInteractiveUserSignInLogs"}:
        if not ids.get("user_id"):
            return ()
        record = EvidenceRecord(
            **base, hop="H1", evidence_type="sign_in", actor_type="user",
            action_category="sign_in", permission_mode="delegated_permissions",
            outcome="succeeded" if str(row.get("ResultType")) == "0" else "failed" if row.get("ResultType") not in {None, ""} else "unknown",
        )
        return (record, replace(record, hop="H5"))
    if source in {"AADServicePrincipalSignInLogs", "AADManagedIdentitySignInLogs"}:
        if not (ids.get("service_principal_id") or ids.get("app_id")):
            return ()
        actor = "managed_identity" if source == "AADManagedIdentitySignInLogs" else "service_principal"
        return (EvidenceRecord(
            **base, hop="H5", evidence_type="sign_in", actor_type=actor,
            action_category="sign_in", permission_mode="application_permissions",
            outcome="succeeded" if str(row.get("ResultType")) == "0" else "failed" if row.get("ResultType") not in {None, ""} else "unknown",
        ),)
    if source == "CopilotActivity":
        if not (ids.get("agent_id") or ids.get("app_id") or ids.get("conversation_id")):
            return ()
        record = EvidenceRecord(**base, hop="H2", evidence_type="agent_activity", actor_type="agent")
        user_type = str(row.get("ActorUserType", "")).lower()
        if ids.get("user_id") and user_type not in {"system", "application", "serviceprincipal", "service_principal"}:
            return (replace(record, hop="H1", actor_type="user"), record)
        return (record,)
    if source.startswith("App"):
        resource = str(row.get("ResourceId") or row.get("_ResourceId") or "").lower()
        operation = str(row.get("OperationName") or row.get("Name") or "").lower()
        if ids.get("agent_id") and (
            "/providers/microsoft.cognitiveservices/" in resource or operation in {"invoke_agent", "gen_ai.invoke_agent"}
        ):
            hop, actor = "H3", "agent"
        elif operation in {"tools/call", "mcp.tool.call", "mcp.tools.call"}:
            hop, actor = "H4", "unknown"
        else:
            return ()
        return (EvidenceRecord(**base, hop=hop, evidence_type="service_request", actor_type=actor, action_category="execute"),)
    if source == "AzureDiagnostics":
        resource = str(row.get("ResourceId") or row.get("_ResourceId") or "").lower()
        provider = str(row.get("ResourceProvider") or "").lower()
        category = str(row.get("Category") or "").lower()
        operation = str(row.get("OperationName") or "").lower()
        if (
            "microsoft.cognitiveservices" not in resource
            and "microsoft.cognitiveservices" not in provider
            and category not in {"audit", "trace", "requestresponse", "azureopenairequestusage"}
        ):
            return ()
        if not (ids.get("operation_id") or ids.get("correlation_id") or ids.get("request_id")):
            return ()
        return (EvidenceRecord(
            **base, hop="H3", evidence_type="service_request", actor_type="agent",
            action_category="execute", resource_type="service",
            reason_codes=() if "trace" in category or "request" in category or "invoke" in operation else ("target_context_only",),
        ),)
    if source == "ContainerAppConsoleLogs":
        # Application console output is written by the MCP server itself and is
        # never evidence.
        return ()
    if source == "ContainerAppHTTPLogs":
        if not (ids.get("request_id") or row.get("ContainerAppName")):
            return ()
        return (EvidenceRecord(
            **base, hop="H4", evidence_type="service_request", actor_type="unknown",
            action_category="execute", resource_type="service",
            outcome=_http_outcome(row.get("StatusCode")),
            reason_codes=("target_context_only",),
        ),)
    return (EvidenceRecord(
        **base, hop="H7", evidence_type="audit" if source in {"AuditLogs", "AzureActivity"} else "security_context",
        reason_codes=("target_context_only",),
    ),)


def extract_rows(payload):
    if isinstance(payload, list):
        if not all(isinstance(row, dict) for row in payload):
            raise ValueError("invalid_response")
        return payload
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError("invalid_response")
    tables = payload.get("tables")
    if not isinstance(tables, list) or len(tables) != 1:
        raise ValueError("invalid_response")
    table = tables[0]
    if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or not isinstance(table.get("rows"), list):
        raise ValueError("invalid_response")
    columns = table["columns"]
    if not all(isinstance(c, dict) and isinstance(c.get("name"), str) for c in columns):
        raise ValueError("invalid_response")
    names = [c["name"] for c in columns]
    if len(set(names)) != len(names):
        raise ValueError("invalid_response")
    if any(not isinstance(row, list) or len(row) != len(names) for row in table["rows"]):
        raise ValueError("invalid_response")
    return [dict(zip(names, row)) for row in table["rows"]]


def normalize_rows(source, rows):
    if source not in QUERY_PLANS or not isinstance(rows, list):
        raise ValueError("invalid_response")
    records = []
    invalid = False
    for row in rows[:MAX_ROWS]:
        try:
            records.extend(normalize_row(source, row))
        except (ValueError, TypeError, OverflowError):
            invalid = True
    # Rows sort by MatchRank, so the overflow row's rank says what was cut: an
    # event/target row means real truncation; a shared-session row means only
    # surrounding context was cut; a broad actor/app row means nothing exact was.
    overflow = _match_rank(rows[MAX_ROWS]) if len(rows) > MAX_ROWS else None
    truncated = overflow is not None and overflow <= SEED_MAX_RANK
    reasons = []
    if overflow == RANK_CONTEXT:
        reasons.append("context_row_limit")
    if invalid:
        reasons.append("invalid_record")
    if truncated:
        reasons.append("row_limit")
    if source == "ContainerAppConsoleLogs" and rows:
        # Console audit lines are written by the MCP server itself.
        reasons.append("self_reported_only")
    elif not records and rows and not invalid:
        reasons.append("schema_unavailable")
    if not rows:
        reasons.append("no_matches")
    return QueryOutcome(
        source, "failed" if invalid else "succeeded", tuple(records), tuple(reasons),
        truncated=truncated, required=QUERY_PLANS[source].required,
    )


MAX_CONTEXT_SEED_ROWS = 10


def _match_rank(row):
    """Rows without a rank (fixtures, older callers) count as event-level matches."""
    value = row.get("MatchRank") if isinstance(row, dict) else None
    try:
        return 0 if value is None else int(value)
    except (TypeError, ValueError):
        return 0


def _seed_rows(rows):
    """Event/target rows always seed; session-level rows only while the session is small."""
    rows = rows[:MAX_ROWS]
    seeds = [row for row in rows if _match_rank(row) <= SEED_MAX_RANK]
    context = [row for row in rows if _match_rank(row) == RANK_CONTEXT]
    return seeds, (context if len(context) <= MAX_CONTEXT_SEED_ROWS else [])


def _discovery_keys(source, rows):
    """Validated identifiers may guide later queries without becoming evidence."""
    identifiers = set()
    for row in rows[:MAX_ROWS]:
        for kind, values in _identifiers(source, row).items():
            if kind not in {"source_event_id", "target_id", "resource_id"}:
                identifiers.update(values)
    return identifiers


def classify_failure(stderr):
    message = stderr.lower()
    if any(term in message for term in ("failed to resolve table", "failed to resolve scalar", "does not refer to any known table", "table not found", "table does not exist", "unknown column")):
        return "unavailable", "source_unavailable"
    if any(term in message for term in ("forbidden", "authorizationfailed", "insufficientaccess", "access denied", "unauthorized", "az login", "aadsts")):
        return "failed", "access_denied"
    return "failed", "query_failed"


def _transient_cli_failure(stderr):
    message = stderr.lower()
    return any(term in message for term in (
        "connection aborted",
        "connectionreseterror",
        "forcibly closed by the remote host",
        "unexpected_eof_while_reading",
        "temporarily unavailable",
        "service unavailable",
    ))


def _prioritized(keys):
    return [value for _, value in sorted((priority, value) for value, priority in keys.items())]


class AzureCLIAdapter:
    def __init__(self, runner=None, workspace=None, command=None, timeout=QUERY_TIMEOUT):
        self.runner = runner or bounded_runner
        self.configuration_error = None
        self.workspace = workspace
        if workspace is None:
            try:
                self.workspace = WorkspaceConfig.from_environment()
            except WorkspaceConfigurationError as error:
                # Keep local health available, but fail every investigation explicitly.
                self.configuration_error = error.reason
        self.command = command if command is not None else cli_command()
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid_execution_bounds")
        self.timeout = min(timeout, QUERY_TIMEOUT)

    @property
    def ready(self):
        return self.workspace is not None and bool(self.command)

    def _run(self, args, timeout):
        result = self.runner(
            [*self.command, *args], capture_output=True, text=True, check=False,
            shell=False, timeout=timeout, max_output_bytes=MAX_OUTPUT_BYTES,
        )
        stdout, stderr = result.stdout or "", result.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise ValueError("invalid_response")
        if len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES or len(stderr.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise OutputLimitExceeded("output_limit")
        return result.returncode, stdout, stderr

    def _read_guid(self, args, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, ("failed", "budget_exhausted")
        try:
            code, output, error = self._run(args, min(self.timeout, remaining))
            if code:
                return None, classify_failure(error)
            value = json.loads(output)
            if not isinstance(value, str) or not re.fullmatch(_GUID, value):
                return None, ("failed", "invalid_response")
            return value, None
        except subprocess.TimeoutExpired:
            return None, ("failed", "timeout")
        except OutputLimitExceeded:
            return None, ("failed", "output_limit")
        except OSError:
            return None, ("unavailable", "cli_unavailable")
        except (ValueError, UnicodeError, RecursionError):
            return None, ("failed", "invalid_response")

    def _resolve_workspace(self, deadline):
        customer_id = self.workspace.customer_id
        workspace_failure = None
        if not self.command:
            workspace_failure = ("unavailable", "cli_unavailable")
        elif self.workspace.tenant_id is not None:
            tenant_id, workspace_failure = self._read_guid([
                "account", "show", "--subscription", self.workspace.subscription_id,
                "--query", "tenantId", "--output", "json", "--only-show-errors",
            ], deadline)
            if workspace_failure is None and tenant_id.casefold() != self.workspace.tenant_id.casefold():
                raise WorkspaceConfigurationError("tenant_mismatch")
        if workspace_failure is None and customer_id is None:
            customer_id, workspace_failure = self._read_guid([
                "monitor", "log-analytics", "workspace", "show", "--ids", self.workspace.resource_id,
                "--subscription", self.workspace.subscription_id, "--query", "customerId",
                "--output", "json", "--only-show-errors",
            ], deadline)
        return customer_id, workspace_failure

    def requester_lead(self, target, timespan):
        """One bounded query over Microsoft platform logs; see requester_lead.py."""
        request = InvestigationRequest(target, "application_permissions", timespan)
        if self.configuration_error is not None:
            raise WorkspaceConfigurationError(self.configuration_error)
        deadline = time.monotonic() + QUERY_TIMEOUT * 2
        customer_id, failure = self._resolve_workspace(deadline)
        return self._requester_lead(request, customer_id, failure, deadline)

    def investigate_with_lead(self, target, mode, timespan):
        """Resolve the workspace once, then run the lead query beside the main plan."""
        request = InvestigationRequest(target, mode, timespan)
        if self.configuration_error is not None:
            raise WorkspaceConfigurationError(self.configuration_error)
        deadline = time.monotonic() + INVESTIGATION_TIMEOUT
        customer_id, failure = self._resolve_workspace(deadline)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self._requester_lead, request, customer_id, failure, deadline)
            outcomes = self._investigate(request, customer_id, failure, deadline)
            return outcomes, pending.result()

    def _requester_lead(self, request, customer_id, failure, deadline):
        if failure:
            return LeadRows(failure[0], failure[1])
        args = [
            "monitor", "log-analytics", "query",
            "--workspace", customer_id, "--subscription", self.workspace.subscription_id,
            "--analytics-query", build_lead_query(request.target, request.timespan),
            "--timespan", request.timespan, "--output", "json", "--only-show-errors",
        ]
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return LeadRows("failed", "budget_exhausted")
            code, output, error = self._run(args, min(self.timeout, remaining))
            if code:
                status, reason = classify_failure(error)
                return LeadRows(status, reason)
            return LeadRows("succeeded", rows=tuple(extract_rows(json.loads(output))))
        except subprocess.TimeoutExpired:
            return LeadRows("failed", "timeout")
        except OutputLimitExceeded:
            return LeadRows("failed", "output_limit")
        except OSError:
            return LeadRows("unavailable", "cli_unavailable")
        except (ValueError, UnicodeError, RecursionError):
            return LeadRows("failed", "invalid_response")

    def investigate(self, target, mode, timespan):
        request = InvestigationRequest(target, mode, timespan)
        if self.configuration_error is not None:
            raise WorkspaceConfigurationError(self.configuration_error)
        deadline = time.monotonic() + INVESTIGATION_TIMEOUT
        customer_id, workspace_failure = self._resolve_workspace(deadline)
        return self._investigate(request, customer_id, workspace_failure, deadline)

    def _investigate(self, request, customer_id, workspace_failure, deadline):
        outcomes = []
        keys = {}
        key_limited = False
        for plan in QUERY_PLANS.values():
            if not plan.native:
                continue
            failure = workspace_failure
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = ("failed", "budget_exhausted")
            if failure:
                outcomes.append(QueryOutcome(plan.source, failure[0], reason_codes=(failure[1],), required=plan.required))
                continue
            query = build_query(plan.source, request.target, request.timespan, _prioritized(keys))
            args = [
                "monitor", "log-analytics", "query",
                "--workspace", customer_id, "--subscription", self.workspace.subscription_id,
                "--analytics-query", query, "--timespan", request.timespan,
                "--output", "json", "--only-show-errors",
            ]
            try:
                try:
                    code, output, error = self._run(args, min(self.timeout, remaining))
                except subprocess.TimeoutExpired:
                    # One retry: a cold workspace cache can push a single query
                    # past its bound while the rerun completes quickly.
                    remaining = deadline - time.monotonic()
                    if remaining <= 1:
                        raise
                    code, output, error = self._run(args, min(self.timeout, remaining))
                if code and _transient_cli_failure(error):
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(0.25, remaining))
                        remaining = deadline - time.monotonic()
                    if remaining > 0:
                        code, output, error = self._run(args, min(self.timeout, remaining))
                if code:
                    status, reason = classify_failure(error)
                    outcome = QueryOutcome(plan.source, status, reason_codes=(reason,), required=plan.required)
                else:
                    payload = json.loads(output)
                    rows = extract_rows(payload)
                    outcome = normalize_rows(plan.source, rows)
            except subprocess.TimeoutExpired:
                outcome = QueryOutcome(plan.source, "failed", reason_codes=("timeout",), required=plan.required)
            except OutputLimitExceeded:
                outcome = QueryOutcome(plan.source, "failed", reason_codes=("output_limit",), required=plan.required)
            except OSError:
                outcome = QueryOutcome(plan.source, "unavailable", reason_codes=("cli_unavailable",), required=plan.required)
            except (ValueError, UnicodeError, RecursionError):
                outcome = QueryOutcome(plan.source, "failed", reason_codes=("invalid_response",), required=plan.required)
            outcomes.append(outcome)
            if outcome.status == "succeeded":
                # Broad actor/app rows stay as evidence but never seed keys, and a
                # large shared session cannot either; otherwise one busy user floods
                # the key set and pushes per-event IDs past the limit.
                seeds, context = _seed_rows(rows)
                for priority, group in ((0, seeds), (1, context)):
                    for raw in _discovery_keys(plan.source, group):
                        if (value := safe_id(raw)) is not None:
                            keys[value] = min(priority, keys.get(value, priority))
                bounded_keys, limited = bound_keys(_prioritized(keys))
                key_limited = key_limited or limited
                keys = {value: keys[value] for value in bounded_keys if value in keys}
        if key_limited:
            outcomes = [
                replace(outcome, truncated=True, reason_codes=(*outcome.reason_codes, "key_limit"))
                for outcome in outcomes
            ]
        return tuple(outcomes)


class InMemoryAdapter:
    """Inject already scoped query results; HTTP input cannot enable fixtures."""

    def __init__(self, outcomes=None, *, tables=None, lead_rows=None):
        if outcomes is not None and tables is not None:
            raise ValueError("provide_outcomes_or_tables")
        self.lead_rows = None if lead_rows is None else tuple(lead_rows)
        if isinstance(outcomes, dict):
            tables, outcomes = outcomes, None
        self.outcomes = tuple(outcomes) if outcomes is not None else None
        self.tables = tables or {}
        self.calls = []
        if self.outcomes is not None and not all(isinstance(item, QueryOutcome) for item in self.outcomes):
            raise ValueError("invalid_outcome")
        if any(source not in QUERY_PLANS for source in self.tables):
            raise ValueError("invalid_source")

    @property
    def ready(self):
        return True

    def requester_lead(self, target, timespan):
        InvestigationRequest(target, "application_permissions", timespan)
        if self.lead_rows is None:
            return LeadRows("unavailable", "source_unavailable")
        return LeadRows("succeeded", rows=self.lead_rows)

    def investigate_with_lead(self, target, mode, timespan):
        return self.investigate(target, mode, timespan), self.requester_lead(target, timespan)

    def investigate(self, target, mode, timespan):
        InvestigationRequest(target, mode, timespan)
        self.calls.append((mode, timespan))
        if self.outcomes is not None:
            return self.outcomes
        return tuple(
            normalize_rows(plan.source, self.tables[plan.source])
            if plan.source in self.tables else QueryOutcome(
                plan.source, "unavailable", reason_codes=("source_unavailable",), required=plan.required,
            )
            for plan in QUERY_PLANS.values()
        )


FixtureAdapter = InMemoryAdapter
