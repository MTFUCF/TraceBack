from __future__ import annotations

from collections import defaultdict
from importlib import import_module


def module(primary: str, fallback: str | None = None):
    try:
        return import_module(primary)
    except ModuleNotFoundError as error:
        if error.name != primary or fallback is None:
            raise
        return import_module(fallback)


def contracts():
    return module("trackback.contracts", "trackback.models")


def correlation():
    return module("trackback.correlation")


def telemetry_query():
    return module("trackback.telemetry", "trackback.queries")


def telemetry_normalization():
    candidate = module("trackback.telemetry", "trackback.adapter")
    if hasattr(candidate, "normalize_row"):
        return candidate
    return module("trackback.adapter")


def azure_cli():
    return module("trackback.azure_cli", "trackback.adapter")


def record(
    hop: str,
    source: str,
    *,
    ids: dict[str, tuple[str, ...] | str] | None = None,
    second: int = 0,
    evidence_type: str = "agent_activity",
    actor_type: str = "unknown",
    action_category: str = "unknown",
    resource_type: str = "unknown",
    permission_mode: str = "unknown",
    outcome: str = "succeeded",
):
    return contracts().EvidenceRecord(
        source=source,
        hop=hop,
        event_time=f"2026-09-05T18:00:{second:02d}Z",
        evidence_type=evidence_type,
        actor_type=actor_type,
        ids=ids or {},
        action_category=action_category,
        resource_type=resource_type,
        permission_mode=permission_mode,
        outcome=outcome,
    )


def delegated_chain():
    return (
        record(
            "H1",
            "CopilotActivity",
            ids={"request_id": "req-human", "user_id": "user-alpha"},
            evidence_type="agent_activity",
            actor_type="user",
            permission_mode="delegated_permissions",
        ),
        record(
            "H2",
            "CopilotActivity",
            ids={"request_id": "req-human", "trace_id": "trace-agent"},
            second=1,
            actor_type="agent",
        ),
        record(
            "H3",
            "AppRequests",
            ids={"trace_id": "trace-agent", "span_id": "span-foundry"},
            second=2,
            evidence_type="service_request",
            actor_type="agent",
            action_category="execute",
        ),
        record(
            "H4",
            "AppDependencies",
            ids={
                "parent_span_id": "span-foundry",
                "request_id": "req-tool",
                "token_id": "token-delegated",
            },
            second=3,
            evidence_type="service_request",
            action_category="execute",
        ),
        record(
            "H5",
            "AADNonInteractiveUserSignInLogs",
            ids={
                "request_id": "req-tool",
                "token_id": "token-delegated",
                "user_id": "user-alpha",
            },
            second=4,
            evidence_type="sign_in",
            actor_type="user",
            action_category="sign_in",
            permission_mode="delegated_permissions",
        ),
        record(
            "H6",
            "MicrosoftGraphActivityLogs",
            ids={
                "token_id": "token-delegated",
                "user_id": "user-alpha",
                "target_id": "target-beta",
            },
            second=5,
            evidence_type="api_action",
            actor_type="user",
            action_category="update",
            resource_type="user",
            permission_mode="delegated_permissions",
        ),
        record(
            "H7",
            "MicrosoftGraphActivityLogs",
            ids={
                "token_id": "token-delegated",
                "user_id": "user-alpha",
                "target_id": "target-beta",
            },
            second=5,
            evidence_type="target",
            actor_type="user",
            action_category="update",
            resource_type="user",
            permission_mode="delegated_permissions",
        ),
    )


def application_chain():
    base = list(delegated_chain())
    base[3] = replace_record_ids(
        base[3],
        {
            "parent_span_id": "span-foundry",
            "request_id": "req-tool",
            "token_id": "token-application",
        },
    )
    base[4] = record(
        "H5",
        "AADServicePrincipalSignInLogs",
        ids={
            "request_id": "req-tool",
            "token_id": "token-application",
            "service_principal_id": "spn-alpha",
            "app_id": "app-alpha",
        },
        second=4,
        evidence_type="sign_in",
        actor_type="service_principal",
        action_category="sign_in",
        permission_mode="application_permissions",
    )
    for index, hop in ((5, "H6"), (6, "H7")):
        base[index] = record(
            hop,
            "MicrosoftGraphActivityLogs",
            ids={
                "token_id": "token-application",
                "service_principal_id": "spn-alpha",
                "app_id": "app-alpha",
                "target_id": "target-beta",
            },
            second=5,
            evidence_type="api_action" if hop == "H6" else "target",
            actor_type="service_principal",
            action_category="update",
            resource_type="user",
            permission_mode="application_permissions",
        )
    return tuple(base)


def replace_record_ids(item, ids):
    contract = contracts()
    return contract.EvidenceRecord(
        source=item.source,
        hop=item.hop,
        event_time=item.event_time,
        evidence_type=item.evidence_type,
        actor_type=item.actor_type,
        ids=ids,
        action_category=item.action_category,
        resource_type=item.resource_type,
        permission_mode=item.permission_mode,
        outcome=item.outcome,
        reason_codes=item.reason_codes,
    )


def outcomes(records, *, failures=()):
    contract = contracts()
    grouped = defaultdict(list)
    for item in records:
        grouped[item.source].append(item)
    result = [
        contract.QueryOutcome(source, "succeeded", tuple(source_records))
        for source, source_records in grouped.items()
    ]
    result.extend(
        contract.QueryOutcome(
            source,
            status,
            reason_codes=(reason,),
            required=required,
        )
        for source, status, reason, required in failures
    )
    return tuple(result)


class StaticAdapter:
    ready = True

    def __init__(self, query_outcomes):
        self.query_outcomes = tuple(query_outcomes)
        self.calls = []

    def investigate(self, target, mode, timespan):
        self.calls.append((target, mode, timespan))
        return self.query_outcomes
