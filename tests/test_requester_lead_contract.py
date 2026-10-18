import json

from tests.contract_support import application_chain, outcomes, record
from trackback.adapter import AzureCLIAdapter, InMemoryAdapter, WorkspaceConfig
from trackback.correlation import correlate
from trackback.requester_lead import LeadRows, build_lead_query

ADMIN = "aaaaaaaa-0000-4000-8000-000000000002"
OTHER_USER = "0b0b0b0b-1111-2222-3333-444444444444"
BLUEPRINT = "bbbbbbbb-0000-4000-8000-000000000001"
OTHER_BLUEPRINT = "bbbbbbbb-0000-4000-8000-000000000002"
AGENT = "cccccccc-0000-4000-8000-000000000001"
OTHER_AGENT = "cccccccc-0000-4000-8000-000000000002"


def coverage(count=5):
    return {"Kind": "coverage", "TimeGenerated": "2026-09-05T19:00:00Z", "DurationMs": count}


def tool(time="2026-09-05T18:00:27Z", duration=5000, blueprint=BLUEPRINT, span="span-1"):
    return {"Kind": "tool", "TimeGenerated": time, "DurationMs": duration, "Blueprint": blueprint,
            "ConversationId": "conv_abc", "SpanId": span}


def agent(identity=AGENT, blueprint=BLUEPRINT):
    return {"Kind": "agent", "TimeGenerated": "2026-09-05T18:01:00Z", "Blueprint": blueprint,
            "AgentIdentity": identity}


def prompt(user=ADMIN, identity=AGENT, time="2026-09-05T17:59:55Z", user_type="Regular"):
    return {"Kind": "prompt", "TimeGenerated": time, "AgentIdentity": identity,
            "UserId": user, "UserType": user_type}


def graph(mode="application_permissions", second=30):
    ids = {"token_id": "tok", "service_principal_id": "mi-1", "app_id": "app-1"}
    if mode == "delegated_permissions":
        ids = {"token_id": "tok", "user_id": ADMIN, "app_id": "app-1"}
    return record(
        "H6", "MicrosoftGraphActivityLogs", ids=ids, second=second, evidence_type="api_action",
        actor_type="user" if mode == "delegated_permissions" else "service_principal",
        action_category="read", resource_type="user", permission_mode=mode,
    )


def lead_for(rows, mode="application_permissions"):
    result = correlate(outcomes((graph(mode),)), mode, lead=LeadRows("succeeded", rows=tuple(rows)))
    return result, result["last_hop"]["requester_lead"]


def test_app_only_lead_names_sole_prompting_user_without_changing_verdict():
    result, lead = lead_for((coverage(), tool(), agent(), prompt()))

    assert lead["status"] == "sole_candidate"
    assert lead["proof"] is False
    assert lead["requires"] == "Application Insights connected to the Foundry project"
    assert [link["join"] for link in lead["links"]] == [
        "target_and_time", "blueprint_exact", "agent_identity_exact", "agent_and_time",
    ]
    assert lead["user_refs"] and lead["user_refs"][0].startswith("corr-")
    assert lead["actions"][0]["prompt_offset_seconds"] == 32.0
    assert result["last_hop"]["verdict"] == "workload_identified"
    assert result["last_hop"]["human"] == "not_in_api_telemetry"
    assert result["human_attribution"] == "unresolved"
    assert ADMIN not in json.dumps(result) and BLUEPRINT not in json.dumps(result)


def test_two_users_prompting_the_same_agent_are_multiple_candidates():
    _, lead = lead_for((coverage(), tool(), agent(), prompt(), prompt(OTHER_USER, time="2026-09-05T18:00:10Z")))

    assert lead["status"] == "multiple_candidates"
    assert len(lead["user_refs"]) == 2


def test_other_agents_prompts_and_blueprints_are_ignored():
    rows = (
        coverage(), tool(), agent(), agent(OTHER_AGENT, OTHER_BLUEPRINT),
        prompt(), prompt(OTHER_USER, identity=OTHER_AGENT),
    )
    _, lead = lead_for(rows)

    assert lead["status"] == "sole_candidate"
    assert len(lead["user_refs"]) == 1


def test_tool_span_outside_graph_time_is_not_used():
    _, lead = lead_for((coverage(), tool(time="2026-09-05T18:00:00Z", duration=1000), agent(), prompt()))

    assert lead["status"] == "no_tool_span"
    assert lead["user_refs"] == []


def test_missing_agent_identity_or_prompts_stop_the_lead():
    _, no_agent = lead_for((coverage(), tool(), prompt()))
    _, no_prompt = lead_for((coverage(), tool(), agent()))
    _, stale = lead_for((coverage(), tool(), agent(), prompt(time="2026-09-05T17:30:00Z")))
    _, non_human = lead_for((coverage(), tool(), agent(), prompt(user_type="System")))

    assert no_agent["status"] == "no_agent_identity"
    assert no_prompt["status"] == "no_agent_prompts"
    assert stale["status"] == "no_agent_prompts"
    assert non_human["status"] == "no_agent_prompts"


def test_no_application_insights_telemetry_is_reported_as_unavailable():
    _, empty = lead_for(())
    _, zero = lead_for((coverage(0), tool(), agent(), prompt()))
    result = correlate(outcomes((graph(),)), "application_permissions", lead=LeadRows("failed", "timeout"))

    assert empty["status"] == "application_insights_unavailable"
    assert zero["status"] == "application_insights_unavailable"
    assert result["last_hop"]["requester_lead"]["status"] == "query_failed"
    assert result["last_hop"]["requester_lead"]["reason_codes"] == ["timeout"]


def test_delegated_lead_cross_checks_against_the_token_user():
    _, agrees = lead_for((coverage(), tool(), agent(), prompt()), "delegated_permissions")
    _, disagrees = lead_for((coverage(), tool(), agent(), prompt(OTHER_USER)), "delegated_permissions")

    assert agrees["agrees_with_token"] is True
    assert disagrees["agrees_with_token"] is False


def test_lead_is_absent_unless_requested_and_invalid_rows_are_flagged():
    chain = application_chain()
    plain = correlate(outcomes((chain[4], chain[5])), "application_permissions")
    _, lead = lead_for((coverage(), tool(), agent(), prompt(), {"Kind": "tool", "TimeGenerated": "bad"}))

    assert "requester_lead" not in plain["last_hop"]
    assert lead["status"] == "sole_candidate"
    assert lead["reason_codes"] == ["invalid_record"]


def test_lead_query_uses_only_platform_tables_and_encodes_the_target():
    query = build_lead_query("evil\"|union x@contoso.com", "P7D")

    for table in ("AppDependencies", "AADServicePrincipalSignInLogs", "CopilotActivity"):
        assert table in query
    assert "ContainerAppConsoleLogs" not in query
    assert "evil" not in query and "base64_decode_tostring" in query
    assert "parentAppId" in query and "CopilotInteraction" in query


def test_in_memory_adapter_reports_lead_unavailable_without_rows():
    assert InMemoryAdapter({}).requester_lead("a@b.com", "P7D").status == "unavailable"
    assert InMemoryAdapter({}, lead_rows=[coverage()]).requester_lead("a@b.com", "P7D").rows == (coverage(),)


def test_cli_adapter_runs_one_bounded_lead_query():
    calls = []

    class Result:
        returncode, stderr = 0, ""
        stdout = json.dumps([coverage(), tool(), agent(), prompt()])

    def runner(args, **kwargs):
        calls.append(args)
        assert kwargs["shell"] is False and kwargs["timeout"] <= 20
        return Result()

    workspace = WorkspaceConfig(
        "/subscriptions/22222222-2222-4222-8222-222222222222/resourceGroups/rg-test/providers/"
        "Microsoft.OperationalInsights/workspaces/ws-test",
        "11111111-1111-4111-8111-111111111111",
    )
    lead = AzureCLIAdapter(runner=runner, workspace=workspace, command=["az"]).requester_lead("a@b.com", "P7D")

    assert lead.status == "succeeded" and len(lead.rows) == 4
    assert len(calls) == 1 and "--analytics-query" in calls[0]
