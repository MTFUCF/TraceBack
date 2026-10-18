"""Internal evidence types. Raw identifiers are never JSON response fields."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping
import re
import unicodedata

MODES = frozenset({"delegated_permissions", "application_permissions"})
TIMESPANS = MappingProxyType({"P7D": "7d", "P30D": "30d", "P90D": "90d"})
MAX_TARGET_LENGTH = 256
HOPS = (
    ("H1", "original_user", "Original human requester"),
    ("H2", "copilot_agent", "Copilot or originating agent"),
    ("H3", "foundry_agent", "Foundry agent"),
    ("H4", "mcp_tool", "MCP-hosting service"),
    ("H5", "workload_identity", "Workload identity or token subject"),
    ("H6", "api_action", "API action"),
    ("H7", "target", "Affected target"),
)
SOURCES = frozenset({
    "CopilotActivity", "SigninLogs", "AADNonInteractiveUserSignInLogs",
    "AADServicePrincipalSignInLogs", "AADManagedIdentitySignInLogs",
    "MicrosoftGraphActivityLogs", "AuditLogs", "AzureActivity", "SecurityAlert",
    "SecurityIncident", "AppRequests", "AppDependencies", "AppTraces",
    "AppEvents", "AppExceptions", "AzureDiagnostics",
    "ContainerAppConsoleLogs", "ContainerAppHTTPLogs",
})
ID_TYPES = frozenset({
    "request_id", "original_request_id", "client_request_id", "operation_id", "trace_id", "span_id",
    "parent_span_id", "correlation_id", "session_id", "conversation_id",
    "thread_id", "message_id", "task_id", "token_id", "user_id", "app_id",
    "service_principal_id", "resource_service_principal_id", "agent_id",
    "target_agent_id", "target_id", "resource_id", "source_event_id",
})
ACTOR_TYPES = frozenset({"user", "service_principal", "managed_identity", "agent", "unknown"})
EVIDENCE_TYPES = frozenset({"sign_in", "agent_activity", "service_request", "api_action", "target", "audit", "security_context"})
ACTION_CATEGORIES = frozenset({"read", "create", "update", "delete", "execute", "sign_in", "unknown"})
RESOURCE_TYPES = frozenset({"user", "group", "application", "service_principal", "managed_identity", "agent", "service", "file", "site", "message", "unknown"})
REASON_CODES = frozenset({
    "observed", "no_matches", "source_unavailable", "access_denied", "query_failed",
    "timeout", "output_limit", "invalid_response", "invalid_record", "row_limit",
    "cli_unavailable", "budget_exhausted", "schema_unavailable", "evidence_gap",
    "exact_identifier", "stable_temporal", "candidate_only", "ambiguous",
    "contradictory_ids", "outside_time_window", "no_binding_keys", "no_shared_ids",
    "persisted_user_required", "backward_human_link_required", "mixed_permissions",
    "permission_mode_mismatch", "unknown_permissions", "incoherent_chain",
    "structurally_unlinkable", "incomplete_coverage", "no_required_source",
    "target_context_only", "unsupported_service_role", "query_not_run",
    "unsuccessful_event", "key_limit", "context_row_limit", "self_reported_only",
})


def safe_id(value):
    if not isinstance(value, str):
        return None
    if not value or value != value.strip() or len(value) > 512 or any(unicodedata.category(c).startswith("C") for c in value):
        return None
    # UUIDs are case-insensitive; arbitrary request/session identifiers are not.
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        return value.lower()
    return value


def utc_timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid_record") from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid_record")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class InvestigationRequest:
    target: str
    mode: str
    timespan: str

    def __post_init__(self):
        if not isinstance(self.target, str) or not 1 <= len(self.target) <= MAX_TARGET_LENGTH:
            raise ValueError("invalid_request")
        if any(unicodedata.category(c).startswith("C") for c in self.target):
            raise ValueError("invalid_request")
        target = unicodedata.normalize("NFC", self.target.strip())
        if not target or len(target) > MAX_TARGET_LENGTH:
            raise ValueError("invalid_request")
        if not isinstance(self.mode, str) or self.mode not in MODES:
            raise ValueError("invalid_request")
        if not isinstance(self.timespan, str) or self.timespan not in TIMESPANS:
            raise ValueError("invalid_request")
        object.__setattr__(self, "target", target)

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or set(payload) != {"target", "mode", "timespan"}:
            raise ValueError("invalid_request")
        return cls(**payload)


@dataclass(frozen=True)
class EvidenceRecord:
    source: str
    hop: str
    event_time: str
    evidence_type: str = "agent_activity"
    actor_type: str = "unknown"
    ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    action_category: str = "unknown"
    resource_type: str = "unknown"
    permission_mode: str = "unknown"
    outcome: str = "unknown"
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self):
        if self.source not in SOURCES or self.hop not in {hop[0] for hop in HOPS}:
            raise ValueError("invalid_record")
        enums = (
            (self.actor_type, ACTOR_TYPES), (self.evidence_type, EVIDENCE_TYPES),
            (self.action_category, ACTION_CATEGORIES), (self.resource_type, RESOURCE_TYPES),
            (self.permission_mode, MODES | {"mixed", "unknown"}),
            (self.outcome, {"succeeded", "failed", "unknown"}),
        )
        if any(value not in choices for value, choices in enums):
            raise ValueError("invalid_record")
        clean = {}
        for kind, values in self.ids.items():
            if kind not in ID_TYPES:
                raise ValueError("invalid_record")
            values = (values,) if isinstance(values, str) else values
            if not isinstance(values, (list, tuple, set, frozenset)) or len(values) > 32:
                raise ValueError("invalid_record")
            identifiers = {safe_id(v) for v in values}
            if None in identifiers:
                raise ValueError("invalid_record")
            if identifiers:
                clean[kind] = tuple(sorted(identifiers))
        if not set(self.reason_codes) <= REASON_CODES:
            raise ValueError("invalid_record")
        object.__setattr__(self, "ids", MappingProxyType(clean))
        object.__setattr__(self, "event_time", utc_timestamp(self.event_time))
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))

    def fingerprint(self):
        return (
            self.hop, self.source, self.event_time, self.evidence_type, self.actor_type,
            tuple(sorted(self.ids.items())), self.action_category, self.resource_type,
            self.permission_mode, self.outcome, self.reason_codes,
        )


@dataclass(frozen=True)
class QueryOutcome:
    source: str
    status: str
    records: tuple[EvidenceRecord, ...] = ()
    reason_codes: tuple[str, ...] = ()
    truncated: bool = False
    required: bool = True

    def __post_init__(self):
        if self.source not in SOURCES or self.status not in {"succeeded", "unavailable", "failed"}:
            raise ValueError("invalid_outcome")
        if not set(self.reason_codes) <= REASON_CODES:
            raise ValueError("invalid_outcome")
        if any(not isinstance(r, EvidenceRecord) or r.source != self.source for r in self.records):
            raise ValueError("invalid_outcome")
        if self.status == "unavailable" and self.records:
            raise ValueError("invalid_outcome")
        unique = {r.fingerprint(): r for r in self.records}
        object.__setattr__(self, "records", tuple(unique[k] for k in sorted(unique)))
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))

    def public(self):
        return {
            "source": self.source, "status": self.status,
            "record_count": len(self.records), "truncated": self.truncated,
            "reason_codes": list(self.reason_codes),
        }
