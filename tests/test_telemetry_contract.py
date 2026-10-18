import json

import pytest

from tests.contract_support import telemetry_normalization, telemetry_query


@pytest.mark.parametrize(
    "hostile",
    (
        "' | union * | where 1 == 1 //",
        ".show tables",
        '<img src=x onerror="globalThis.pwned=true">',
        r"C:\temp\thing'; drop table x",
    ),
)
def test_hostile_target_is_encoded_as_scalar_data_not_kql_source(hostile):
    telemetry = telemetry_query()
    query = telemetry.build_query("MicrosoftGraphActivityLogs", hostile, "P30D")

    assert hostile not in query
    assert "base64_decode_tostring(" in query
    assert "MicrosoftGraphActivityLogs" in query
    assert "take " in query
    assert "TimeGenerated >= ago(30d)" in query
    assert "search *" not in query.lower()
    assert "union *" not in query.lower()


@pytest.mark.parametrize("hostile", ("target\n.show tables", "target\x00hidden", "x" * 257))
def test_control_character_and_oversized_targets_are_rejected_before_query(hostile):
    with pytest.raises(ValueError, match="invalid_request"):
        telemetry_query().build_query("MicrosoftGraphActivityLogs", hostile, "P30D")


def test_plan_id_and_timespan_are_allowlisted():
    telemetry = telemetry_query()

    with pytest.raises((KeyError, ValueError)):
        telemetry.build_query("UserSelectedTable", "safe-target", "P30D")
    with pytest.raises(ValueError, match="invalid_request"):
        telemetry.build_query("MicrosoftGraphActivityLogs", "safe-target", "PT999H")


def test_h3_foundry_and_h4_mcp_query_plans_are_fixed_bounded_and_configurable(monkeypatch):
    telemetry = telemetry_query()

    foundry = telemetry.build_query("AzureDiagnostics", "safe-target", "P7D")
    console = telemetry.build_query("ContainerAppConsoleLogs", "safe-target", "P7D")
    http = telemetry.build_query("ContainerAppHTTPLogs", "safe-target", "P7D")

    assert "AzureDiagnostics" in foundry
    assert "ContainerAppConsoleLogs" in console
    assert "parse_json(" in console
    assert "ContainerAppHTTPLogs" in http
    for query in (foundry, console, http):
        assert "base64_decode_tostring(" in query
        assert "TimeGenerated >= ago(7d)" in query
        assert "take " in query
        assert "union *" not in query.lower()

    monkeypatch.setenv("TRACKBACK_TABLE_CONTAINER_APP_CONSOLE_LOGS", "CustomConsoleLogs")
    configured = telemetry.build_query("ContainerAppConsoleLogs", "safe-target", "P7D")

    assert "\nCustomConsoleLogs\n" in configured
    assert "\nContainerAppConsoleLogs\n" not in configured


def test_target_seeding_is_limited_to_resource_context_not_actor_ids():
    telemetry = telemetry_query()

    graph = telemetry.build_query("MicrosoftGraphActivityLogs", "target-alpha", "P7D")
    sign_in = telemetry.build_query("AADNonInteractiveUserSignInLogs", "target-alpha", "P7D")
    dependencies = telemetry.build_query("AppDependencies", "target-alpha", "P7D")

    assert 'tostring(column_ifexists("RequestUri", "")) contains _target' in graph
    assert 'tostring(column_ifexists("UserId", "")) == _target' not in sign_in
    assert 'tostring(column_ifexists("UserId", "")) in (_keys)' in sign_in
    assert 'tostring(column_ifexists("UserPrincipalName", "")) contains _target' not in sign_in
    assert 'tostring(column_ifexists("Name", "")) contains _target' in dependencies
    assert 'tostring(column_ifexists("Target", "")) contains _target' in dependencies


def test_backward_plan_uses_target_discovery_before_foundry_and_copilot_queries():
    telemetry = telemetry_query()
    order = tuple(telemetry.QUERY_PLANS)

    assert order.index("AppDependencies") < order.index("AppRequests")
    assert order.index("AppRequests") < order.index("CopilotActivity")
    assert telemetry.QUERY_PLANS["ContainerAppConsoleLogs"].required is False


def test_invalid_configured_table_names_fail_before_query(monkeypatch):
    telemetry = telemetry_query()
    monkeypatch.setenv("TRACKBACK_TABLE_CONTAINER_APP_HTTP_LOGS", "Unsafe; union *")

    with pytest.raises(ValueError, match="invalid_table_configuration"):
        telemetry.build_query("ContainerAppHTTPLogs", "safe-target", "P7D")


def test_normalization_preserves_typed_identifiers_and_source_provenance():
    normalizer = telemetry_normalization()
    row = {
        "TimeGenerated": "2026-09-05T18:00:00Z",
        "OperationId": "operation-alpha",
        "Id": "span-alpha",
        "ParentId": "span-parent",
        "ResourceId": "/providers/Microsoft.CognitiveServices/accounts/example",
        "OperationName": "invoke_agent",
        "Properties": json.dumps(
            {
                "trace_id": "trace-alpha",
                "span_id": "span-alpha",
                "gen_ai.agent.id": "agent-alpha",
                "gen_ai.conversation.id": "conversation-alpha",
            }
        ),
    }

    records = normalizer.normalize_row("AppRequests", row)

    assert len(records) == 1
    evidence = records[0]
    assert evidence.source == "AppRequests"
    assert evidence.ids["operation_id"] == ("operation-alpha",)
    assert evidence.ids["trace_id"] == ("trace-alpha",)
    assert evidence.ids["span_id"] == ("span-alpha",)
    assert evidence.ids["parent_span_id"] == ("span-parent",)
    assert evidence.ids["agent_id"] == ("agent-alpha",)
    assert evidence.ids["conversation_id"] == ("conversation-alpha",)
    assert evidence.ids["source_event_id"] == ("AppRequests:span-alpha",)
    assert evidence.ids["operation_id"] != evidence.ids["span_id"]


def test_mcp_console_json_projection_is_discovery_only_not_attribution_evidence():
    normalizer = telemetry_normalization()
    marker = "https://graph.microsoft.com/v1.0/users/target-alpha"
    row = {
        "TimeGenerated": "2026-09-19T17:10:00Z",
        "Event": "graph_request",
        "ToolName": "get_user_profile",
        "AuthMode": "application",
        "GraphRequestUri": marker,
        "HttpStatus": "200",
        "RequestId": "graph-request",
        "ClientRequestId": "graph-client-request",
        "AppId": "app-alpha",
        "PlatformThreadId": "thread-alpha",
        "ResourceId": "/subscriptions/placeholder/resourceGroups/rg/providers/Microsoft.App/containerApps/app",
    }

    records = normalizer.normalize_row("ContainerAppConsoleLogs", row)

    assert records == ()
    assert marker not in repr(records)


def test_unrelated_keyword_match_does_not_manufacture_evidence():
    normalizer = telemetry_normalization()
    rows = [
        {
            "TimeGenerated": "2026-09-05T18:00:00Z",
            "ActorName": "safe-target",
            "AgentName": "safe-target",
            "LLMEventData": json.dumps({"Prompt": "safe-target"}),
        }
    ]

    outcome = normalizer.normalize_rows("CopilotActivity", rows)

    assert outcome.status == "succeeded"
    assert outcome.records == ()
    assert "schema_unavailable" in outcome.reason_codes


def test_hostile_dynamic_bags_are_not_copied_into_normalized_evidence():
    normalizer = telemetry_normalization()
    marker = '<svg onload="globalThis.pwned=true">'
    rows = [
        {
            "TimeGenerated": "2026-09-05T18:00:00Z",
            "OperationId": "operation-alpha",
            "Id": "span-alpha",
            "ResourceId": "/providers/Microsoft.CognitiveServices/accounts/example",
            "OperationName": "invoke_agent",
            "Properties": json.dumps(
                {
                    "trace_id": "trace-alpha",
                    "gen_ai.agent.id": "agent-alpha",
                    "request_body": marker,
                    "credential": "not-for-output",
                }
            ),
            "RawData": marker,
        }
    ]

    outcome = normalizer.normalize_rows("AppRequests", rows)

    assert outcome.status == "succeeded"
    assert marker not in repr(outcome.records)
    assert "request_body" not in repr(outcome.records)
    assert "RawData" not in repr(outcome.records)
    assert "credential" not in repr(outcome.records)


@pytest.mark.parametrize(
    "payload",
    (
        "not-json",
        {"error": {"message": "raw service error"}},
        {"tables": []},
        {"tables": [{"columns": [{"name": "x"}, {"name": "x"}], "rows": [[1, 2]]}]},
        {"tables": [{"columns": [{"name": "x"}], "rows": [[1, 2]]}]},
    ),
)
def test_malformed_cli_payload_shapes_are_rejected(payload):
    normalizer = telemetry_normalization()

    with pytest.raises(ValueError, match="invalid_response"):
        normalizer.extract_rows(payload)


def test_control_characters_in_typed_ids_fail_closed_without_raw_row_output():
    normalizer = telemetry_normalization()
    outcome = normalizer.normalize_rows(
        "AppRequests",
        [
            {
                "TimeGenerated": "2026-09-05T18:00:00Z",
                "OperationId": "operation\x00hidden",
                "Id": "span-alpha",
                "ResourceId": "/providers/Microsoft.CognitiveServices/accounts/example",
                "OperationName": "invoke_agent",
            }
        ],
    )

    assert outcome.status == "failed"
    assert outcome.records == ()
    assert outcome.reason_codes == ("invalid_record",)
    assert "operation\x00hidden" not in repr(outcome.public())


@pytest.mark.parametrize("flag", (False, 0, "", "false", "False", None))
def test_cli_false_invalid_identifier_values_do_not_reject_valid_rows(flag):
    normalizer = telemetry_normalization()
    row = {
        "TimeGenerated": "2026-09-05T18:00:00Z",
        "UserId": "user-alpha",
        "ResultType": "0",
        "InvalidIdentifier": flag,
    }

    records = normalizer.normalize_row("AADNonInteractiveUserSignInLogs", row)

    assert [record.hop for record in records] == ["H1", "H5"]
    assert all(record.outcome == "succeeded" for record in records)


@pytest.mark.parametrize("flag", (True, 1, "true", "True", "unexpected"))
def test_true_or_malformed_invalid_identifier_values_fail_closed(flag):
    normalizer = telemetry_normalization()
    row = {
        "TimeGenerated": "2026-09-05T18:00:00Z",
        "UserId": "user-alpha",
        "ResultType": "0",
        "InvalidIdentifier": flag,
    }

    with pytest.raises(ValueError, match="invalid_record"):
        normalizer.normalize_row("AADNonInteractiveUserSignInLogs", row)


def test_row_limit_is_explicit_and_does_not_inflate_record_count():
    normalizer = telemetry_normalization()
    query = telemetry_query()
    rows = [
        {
            "TimeGenerated": f"2026-09-05T18:{index // 60:02d}:{index % 60:02d}Z",
            "OperationId": f"operation-{index}",
            "Id": f"span-{index}",
            "AgentId": "agent-alpha",
            "ResourceId": "/providers/Microsoft.CognitiveServices/accounts/example",
            "OperationName": "invoke_agent",
        }
        for index in range(query.MAX_ROWS + 5)
    ]

    outcome = normalizer.normalize_rows("AppRequests", rows)

    assert outcome.truncated is True
    assert "row_limit" in outcome.reason_codes
    assert len(outcome.records) == query.MAX_ROWS


def test_target_search_covers_percent_encoded_upn_and_resolved_object_id():
    query = telemetry_query().build_query("MicrosoftGraphActivityLogs", "person@example.invalid", "P7D")

    assert "person@example.invalid" not in query
    assert "let _target_enc = url_encode_component(_target);" in query
    assert 'tostring(column_ifexists("RequestUri", "")) contains _target_enc' in query
    assert "union isfuzzy=true (datatable(AccountObjectId:string)[]), (IdentityInfo" in query
    assert 'isnotempty(_target_oid) and tostring(column_ifexists("RequestUri", "")) contains _target_oid' in query


def test_mcp_console_projection_exposes_token_and_trace_binding_keys_for_discovery():
    query = telemetry_query().build_query("ContainerAppConsoleLogs", "safe-target", "P7D", keys=("outbound-uti",))

    assert "\nContainerAppConsoleLogs\n" in query
    assert "OutboundTokenId = tostring(coalesce(" in query
    assert "InboundTokenId = tostring(coalesce(" in query
    assert "TraceId = tostring(split(" in query
    assert ".outbound_token_claims, dynamic({})).uti)) in (_keys)" in query


def test_mcp_console_token_ids_become_discovery_keys_but_not_evidence():
    from trackback.adapter import _discovery_keys

    row = {
        "TimeGenerated": "2026-10-10T17:10:00Z",
        "ClientRequestId": "client-alpha",
        "OutboundTokenId": "outbound-uti",
        "InboundTokenId": "inbound-uti",
        "TraceId": "0af7651916cd43dd8448eb211c80319c",
        "UserId": "user-alpha",
    }

    keys = _discovery_keys("ContainerAppConsoleLogs", [row])

    assert {"client-alpha", "outbound-uti", "inbound-uti", "0af7651916cd43dd8448eb211c80319c", "user-alpha"} <= keys
    assert telemetry_normalization().normalize_row("ContainerAppConsoleLogs", row) == ()


def test_exact_identifier_rows_rank_ahead_of_broad_actor_matches():
    from trackback.queries import build_query

    query = build_query("AADNonInteractiveUserSignInLogs", "user@example.test", "P7D", ["key-alpha"])
    rank = next(line for line in query.splitlines() if line.startswith("| extend MatchRank"))

    assert 'column_ifexists("UniqueTokenIdentifier", "")) in (_keys)' in rank
    assert '"UserId"' not in rank and '"AppId"' not in rank
    assert rank.startswith("| extend MatchRank = case(")
    token = rank.index('"UniqueTokenIdentifier"')
    session = rank.index('"SessionId"')
    assert token < session
    graph = next(line for line in build_query("MicrosoftGraphActivityLogs", "user@example.test", "P7D", ["k"]).splitlines()
                 if line.startswith("| extend MatchRank"))
    assert graph.index('"SignInActivityId"') < graph.index("contains _target")
    assert rank.rstrip().endswith(", 3)")
    assert "| order by MatchRank asc, TimeGenerated asc" in query
    assert query.index("| order by MatchRank") < query.index("| take ")


def test_console_audit_rows_are_reported_as_self_reported_not_schema_gap():
    from trackback.adapter import normalize_rows

    outcome = normalize_rows("ContainerAppConsoleLogs", [{
        "TimeGenerated": "2026-09-05T18:00:00Z", "MatchRank": 0, "Event": "graph_request",
        "OutboundTokenId": "outbound-uti", "ClientRequestId": "client-alpha",
    }])

    assert outcome.records == ()
    assert outcome.reason_codes == ("self_reported_only",)


def test_overflow_of_broad_matches_only_is_not_truncation():
    from trackback.adapter import normalize_rows
    from trackback.queries import MAX_ROWS

    def row(i, rank):
        return {"TimeGenerated": f"2026-09-05T18:{i // 60:02d}:{i % 60:02d}Z", "MatchRank": rank,
                "UserId": "11111111-1111-1111-1111-111111111111", "UniqueTokenIdentifier": f"t{i}", "ResultType": "0"}

    broad_overflow = [row(0, 0)] + [row(i, 3) for i in range(1, MAX_ROWS + 1)]
    context_overflow = [row(0, 0)] + [row(i, 2) for i in range(1, MAX_ROWS + 1)]
    target_overflow = [row(0, 0)] + [row(i, 1) for i in range(1, MAX_ROWS + 1)]
    exact_overflow = [row(i, 0) for i in range(MAX_ROWS + 1)]

    assert normalize_rows("SigninLogs", broad_overflow).truncated is False
    assert normalize_rows("SigninLogs", broad_overflow).reason_codes == ()
    context = normalize_rows("SigninLogs", context_overflow)
    assert context.truncated is False and context.reason_codes == ("context_row_limit",)
    assert normalize_rows("SigninLogs", target_overflow).truncated is True
    assert normalize_rows("SigninLogs", exact_overflow).truncated is True
    assert "row_limit" in normalize_rows("SigninLogs", exact_overflow).reason_codes
