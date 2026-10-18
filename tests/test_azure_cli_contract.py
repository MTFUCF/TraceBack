import json
import subprocess
import base64

import pytest

from tests.contract_support import azure_cli, correlation, telemetry_query


CUSTOMER_ID = "11111111-1111-4111-8111-111111111111"
RESOURCE_ID = (
    "/subscriptions/22222222-2222-4222-8222-222222222222/"
    "resourceGroups/rg-test/providers/Microsoft.OperationalInsights/workspaces/ws-test"
)
TENANT_ID = "abcdefab-3333-4333-8333-333333333333"


class RecordingRunner:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((tuple(args), dict(kwargs)))
        return self.behavior(args, kwargs)


def completed(args, *, code=0, stdout="[]", stderr=""):
    return subprocess.CompletedProcess(args, code, stdout, stderr)


def adapter_for(runner, *, command=("az-test",)):
    cli = azure_cli()
    workspace = cli.WorkspaceConfig(RESOURCE_ID, CUSTOMER_ID)
    return cli.AzureCLIAdapter(
        runner=runner,
        workspace=workspace,
        command=list(command),
        timeout=1,
    )


def test_every_cli_call_is_read_only_bounded_and_explicitly_scoped():
    runner = RecordingRunner(lambda args, kwargs: completed(args))
    adapter = adapter_for(runner)

    adapter.investigate("target-alpha", "delegated_permissions", "P7D")

    assert runner.calls
    for args, kwargs in runner.calls:
        joined = " ".join(args)
        assert args[:4] == ("az-test", "monitor", "log-analytics", "query")
        assert "--workspace" in args and CUSTOMER_ID in args
        assert "--subscription" in args and "22222222-2222-4222-8222-222222222222" in args
        assert "--analytics-query" in args
        assert "--only-show-errors" in args
        assert "account show" not in joined
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        assert 0 < kwargs["timeout"] <= 1


def test_malformed_cli_json_is_an_explicit_invalid_response_for_each_source():
    runner = RecordingRunner(lambda args, kwargs: completed(args, stdout="{broken"))

    outcomes = adapter_for(runner).investigate(
        "target-alpha", "delegated_permissions", "P30D"
    )

    assert outcomes
    assert all(outcome.status == "failed" for outcome in outcomes)
    assert all(outcome.reason_codes == ("invalid_response",) for outcome in outcomes)


def test_nonzero_exit_is_not_converted_to_empty_success():
    marker = "opaque-cli-stderr-must-not-escape"
    runner = RecordingRunner(
        lambda args, kwargs: completed(args, code=7, stderr=marker)
    )

    outcomes = adapter_for(runner).investigate(
        "target-alpha", "delegated_permissions", "P30D"
    )

    assert outcomes
    assert all(outcome.status == "failed" for outcome in outcomes)
    assert all(outcome.reason_codes == ("query_failed",) for outcome in outcomes)
    assert marker not in repr([outcome.public() for outcome in outcomes])


def test_transient_query_connection_failure_is_retried_once():
    attempts = {}

    def transient(args, kwargs):
        query = args[args.index("--analytics-query") + 1]
        source = next(
            source for source in telemetry_query().QUERY_PLANS
            if f"\n{source}\n" in query
        )
        attempts[source] = attempts.get(source, 0) + 1
        if source == "AppDependencies" and attempts[source] == 1:
            return completed(
                args, code=1,
                stderr="Connection aborted: ConnectionResetError forcibly closed by the remote host",
            )
        return completed(args)

    outcomes = adapter_for(RecordingRunner(transient)).investigate(
        "target-alpha", "delegated_permissions", "P7D"
    )

    dependency = next(outcome for outcome in outcomes if outcome.source == "AppDependencies")
    assert dependency.status == "succeeded"
    assert attempts["AppDependencies"] == 2
    assert all(count == 1 for source, count in attempts.items() if source != "AppDependencies")


def test_access_denied_stderr_is_classified_without_exposing_stderr():
    marker = "Forbidden: tenant-secret-detail"
    runner = RecordingRunner(
        lambda args, kwargs: completed(args, code=1, stderr=marker)
    )

    outcomes = adapter_for(runner).investigate(
        "target-alpha", "application_permissions", "P30D"
    )

    assert all(outcome.status == "failed" for outcome in outcomes)
    assert all(outcome.reason_codes == ("access_denied",) for outcome in outcomes)
    assert marker not in repr([outcome.public() for outcome in outcomes])


def test_missing_executable_marks_all_sources_unavailable_without_calling_runner():
    runner = RecordingRunner(lambda args, kwargs: completed(args))

    outcomes = adapter_for(runner, command=()).investigate(
        "target-alpha", "delegated_permissions", "P30D"
    )

    assert runner.calls == []
    assert outcomes
    assert all(outcome.status == "unavailable" for outcome in outcomes)
    assert all(outcome.reason_codes == ("cli_unavailable",) for outcome in outcomes)


def test_timeout_is_explicit_for_every_attempted_source():
    def timeout(args, kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    outcomes = adapter_for(RecordingRunner(timeout)).investigate(
        "target-alpha", "delegated_permissions", "P30D"
    )

    assert outcomes
    assert all(outcome.status == "failed" for outcome in outcomes)
    assert all(outcome.reason_codes == ("timeout",) for outcome in outcomes)


def test_single_query_timeout_is_retried_once():
    attempts = {}

    def flaky(args, kwargs):
        if "--analytics-query" not in args:
            return completed(args, stdout=json.dumps([]))
        query = args[args.index("--analytics-query") + 1]
        if "\nSigninLogs\n" in query:
            attempts["SigninLogs"] = attempts.get("SigninLogs", 0) + 1
            if attempts["SigninLogs"] == 1:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return completed(args, stdout=json.dumps([]))

    outcomes = adapter_for(RecordingRunner(flaky)).investigate("target-alpha", "delegated_permissions", "P7D")

    sign_ins = next(o for o in outcomes if o.source == "SigninLogs")
    assert attempts["SigninLogs"] == 2
    assert sign_ins.status == "succeeded" and "timeout" not in sign_ins.reason_codes


def test_partial_source_failure_remains_visible_to_correlation():
    def partial(args, kwargs):
        query = args[args.index("--analytics-query") + 1]
        if "\nAppDependencies\n" in query:
            return completed(args, code=1, stderr="temporary service failure")
        return completed(args, stdout=json.dumps([]))

    outcomes = adapter_for(RecordingRunner(partial)).investigate(
        "target-alpha", "delegated_permissions", "P30D"
    )
    function = getattr(correlation(), "correlate", correlation().correlate_chain)
    result = function(outcomes, "delegated_permissions")

    assert any(
        outcome.source == "AppDependencies"
        and outcome.status == "failed"
        and outcome.reason_codes == ("query_failed",)
        for outcome in outcomes
    )
    assert any(outcome.status == "succeeded" for outcome in outcomes)
    assert result["status"] == "partial"
    assert "incomplete_coverage" in result["reason_codes"]


def test_valid_non_evidence_rows_seed_later_queries_without_entering_results():
    seen_app_traces_keys = []

    def discovery(args, kwargs):
        query = args[args.index("--analytics-query") + 1]
        if "\nAppDependencies\n" in query:
            return completed(args, stdout=json.dumps([{
                "TimeGenerated": "2026-09-05T18:00:00Z",
                "OperationId": "operation-discovery",
                "Id": "dependency-span",
                "Name": "GET /v1.0/users/target-alpha",
                "Target": "graph.microsoft.com",
                "ResourceId": "/providers/Microsoft.Insights/components/example",
                "InvalidIdentifier": "False",
            }]))
        if "\nAppTraces\n" in query:
            encoded = query.split("let _keys = todynamic(base64_decode_tostring('", 1)[1].split("'));", 1)[0]
            seen_app_traces_keys.extend(json.loads(base64.b64decode(encoded).decode("utf-8")))
        return completed(args, stdout=json.dumps([]))

    outcomes = adapter_for(RecordingRunner(discovery)).investigate(
        "target-alpha", "delegated_permissions", "P7D"
    )

    dependency = next(outcome for outcome in outcomes if outcome.source == "AppDependencies")
    assert dependency.status == "succeeded"
    assert dependency.records == ()
    assert "schema_unavailable" in dependency.reason_codes
    assert "operation-discovery" in seen_app_traces_keys
    assert "dependency-span" in seen_app_traces_keys


def test_rows_matched_only_by_broad_identity_do_not_seed_later_queries():
    seen = []

    def discovery(args, kwargs):
        query = args[args.index("--analytics-query") + 1]
        if "\nMicrosoftGraphActivityLogs\n" in query:
            return completed(args, stdout=json.dumps([{
                "TimeGenerated": "2026-09-05T18:00:00Z", "MatchRank": 0,
                "UserId": "11111111-1111-1111-1111-111111111111", "SignInActivityId": "token-exact",
                "Scopes": "User.ReadBasic.All", "RequestMethod": "GET", "ResponseStatusCode": "200",
                "RequestUri": "https://graph.microsoft.com/v1.0/users/target-alpha",
            }]))
        if "\nAADNonInteractiveUserSignInLogs\n" in query:
            return completed(args, stdout=json.dumps([
                {"TimeGenerated": "2026-09-05T17:59:59Z", "MatchRank": 0, "ResultType": "0",
                 "UserId": "11111111-1111-1111-1111-111111111111", "UniqueTokenIdentifier": "token-exact",
                 "SessionId": "session-exact"},
                {"TimeGenerated": "2026-09-04T10:00:00Z", "MatchRank": 3, "ResultType": "0",
                 "UserId": "11111111-1111-1111-1111-111111111111", "UniqueTokenIdentifier": "token-unrelated",
                 "SessionId": "session-unrelated"},
            ]))
        if "\nSigninLogs\n" in query:
            encoded = query.split("let _keys = todynamic(base64_decode_tostring('", 1)[1].split("'));", 1)[0]
            seen.extend(json.loads(base64.b64decode(encoded).decode("utf-8")))
        return completed(args, stdout=json.dumps([]))

    outcomes = adapter_for(RecordingRunner(discovery)).investigate("target-alpha", "delegated_permissions", "P7D")

    sign_ins = next(o for o in outcomes if o.source == "AADNonInteractiveUserSignInLogs")
    assert len({r.event_time for r in sign_ins.records}) == 2  # both remain evidence
    assert "session-exact" in seen and "token-exact" in seen
    assert "session-unrelated" not in seen and "token-unrelated" not in seen


def test_large_shared_session_does_not_seed_keys_but_small_one_does():
    from trackback.adapter import MAX_CONTEXT_SEED_ROWS

    def run(context_rows):
        seen = []

        def discovery(args, kwargs):
            query = args[args.index("--analytics-query") + 1]
            if "\nMicrosoftGraphActivityLogs\n" in query:
                return completed(args, stdout=json.dumps([{
                    "TimeGenerated": "2026-09-05T18:00:00Z", "MatchRank": 0,
                    "UserId": "11111111-1111-1111-1111-111111111111", "SignInActivityId": "token-exact",
                    "Scopes": "User.ReadBasic.All", "RequestMethod": "GET", "ResponseStatusCode": "200",
                    "RequestUri": "https://graph.microsoft.com/v1.0/users/target-alpha",
                }]))
            if "\nAADNonInteractiveUserSignInLogs\n" in query:
                return completed(args, stdout=json.dumps([
                    {"TimeGenerated": f"2026-09-05T17:{i:02d}:00Z", "MatchRank": 2, "ResultType": "0",
                     "UserId": "11111111-1111-1111-1111-111111111111",
                     "UniqueTokenIdentifier": f"session-token-{i}", "SessionId": "session-shared"}
                    for i in range(context_rows)
                ]))
            if "\nSigninLogs\n" in query:
                encoded = query.split("let _keys = todynamic(base64_decode_tostring('", 1)[1].split("'));", 1)[0]
                seen.extend(json.loads(base64.b64decode(encoded).decode("utf-8")))
            return completed(args, stdout=json.dumps([]))

        adapter_for(RecordingRunner(discovery)).investigate("target-alpha", "delegated_permissions", "P7D")
        return seen

    assert "session-token-0" in run(2)
    flooded = run(MAX_CONTEXT_SEED_ROWS + 1)
    assert "token-exact" in flooded and "session-token-0" not in flooded


def test_all_sources_unavailable_produces_unavailable_not_impossible():
    outcomes = adapter_for(
        RecordingRunner(lambda args, kwargs: completed(args)),
        command=(),
    ).investigate("target-alpha", "application_permissions", "P30D")
    function = getattr(correlation(), "correlate", correlation().correlate_chain)

    result = function(outcomes, "application_permissions")

    assert result["status"] == "unavailable"
    assert result["human_attribution"] == "unresolved"
    assert "no_required_source" in result["reason_codes"]
    assert "structurally_unlinkable" not in result["reason_codes"]


def configured_adapter(runner, *, customer=CUSTOMER_ID, tenant=None, timeout=1):
    cli = azure_cli()
    return cli.AzureCLIAdapter(
        runner=runner,
        workspace=cli.WorkspaceConfig(RESOURCE_ID, customer, tenant),
        command=["az-test"],
        timeout=timeout,
    )


def assert_bounded_call(args, kwargs):
    assert "--subscription" in args
    assert args[args.index("--subscription") + 1] == "22222222-2222-4222-8222-222222222222"
    assert args[args.index("--output") + 1] == "json"
    assert "--only-show-errors" in args
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert 0 < kwargs["timeout"] <= 1
    assert kwargs["max_output_bytes"] == azure_cli().MAX_OUTPUT_BYTES


def test_non_default_environment_customer_id_is_honored_without_lookup(workspace_environment):
    workspace_environment({
        "LOG_ANALYTICS_WORKSPACE_RESOURCE_ID": RESOURCE_ID,
        "LOG_ANALYTICS_WORKSPACE_ID": CUSTOMER_ID,
    })
    runner = RecordingRunner(lambda args, kwargs: completed(args))
    adapter = azure_cli().AzureCLIAdapter(runner=runner, command=["az-test"], timeout=1)

    outcomes = adapter.investigate("target-alpha", "delegated_permissions", "P7D")

    assert all(outcome.status == "succeeded" for outcome in outcomes)
    assert runner.calls
    for args, kwargs in runner.calls:
        assert args[:4] == ("az-test", "monitor", "log-analytics", "query")
        assert args[args.index("--workspace") + 1] == CUSTOMER_ID
        assert_bounded_call(args, kwargs)


def test_missing_customer_id_is_resolved_read_only_before_queries():
    def resolve(args, kwargs):
        if "show" in args:
            return completed(args, stdout=json.dumps(CUSTOMER_ID))
        return completed(args)

    runner = RecordingRunner(resolve)

    outcomes = configured_adapter(runner, customer=None).investigate(
        "target-alpha", "delegated_permissions", "P7D"
    )

    assert all(outcome.status == "succeeded" for outcome in outcomes)
    args, _ = runner.calls[0]
    assert args[:5] == ("az-test", "monitor", "log-analytics", "workspace", "show")
    assert args[args.index("--ids") + 1] == RESOURCE_ID
    assert args[args.index("--query") + 1] == "customerId"
    for args, kwargs in runner.calls:
        assert_bounded_call(args, kwargs)
    for args, _ in runner.calls[1:]:
        assert args[args.index("--workspace") + 1] == CUSTOMER_ID
    assert not any("account" in args for args, _ in runner.calls)


@pytest.mark.parametrize("customer", (None, CUSTOMER_ID))
def test_expected_tenant_is_checked_before_lookup_or_queries(customer):
    def resolve(args, kwargs):
        if args[1:3] == ["account", "show"]:
            return completed(args, stdout=json.dumps(TENANT_ID.upper()))
        if "show" in args:
            return completed(args, stdout=json.dumps(CUSTOMER_ID))
        return completed(args)

    runner = RecordingRunner(resolve)
    adapter = configured_adapter(runner, customer=customer, tenant=TENANT_ID)

    assert adapter.ready
    assert runner.calls == []
    outcomes = adapter.investigate("target-alpha", "delegated_permissions", "P7D")

    assert all(outcome.status == "succeeded" for outcome in outcomes)
    args, _ = runner.calls[0]
    assert args[:3] == ("az-test", "account", "show")
    assert args[args.index("--query") + 1] == "tenantId"
    assert TENANT_ID not in args
    assert sum("workspace" in args and "show" in args for args, _ in runner.calls) == (customer is None)
    for args, kwargs in runner.calls:
        assert_bounded_call(args, kwargs)
    assert TENANT_ID not in repr([outcome.public() for outcome in outcomes])


@pytest.mark.parametrize("customer", (None, CUSTOMER_ID))
def test_tenant_mismatch_blocks_all_workspace_calls_without_account_disclosure(customer):
    wrong_tenant = "ffffffff-4444-4444-8444-444444444444"
    runner = RecordingRunner(
        lambda args, kwargs: completed(args, stdout=json.dumps(wrong_tenant))
    )
    adapter = configured_adapter(runner, customer=customer, tenant=TENANT_ID)

    with pytest.raises(azure_cli().WorkspaceConfigurationError) as caught:
        adapter.investigate("target-alpha", "delegated_permissions", "P7D")

    assert caught.value.reason == "tenant_mismatch"
    assert len(runner.calls) == 1
    assert runner.calls[0][0][:3] == ("az-test", "account", "show")
    for identifier in (wrong_tenant, TENANT_ID, RESOURCE_ID, CUSTOMER_ID):
        assert identifier not in repr(caught.value.public())


def test_tenant_context_is_rechecked_for_every_investigation():
    tenant = [TENANT_ID]

    def resolve(args, kwargs):
        return completed(args, stdout=json.dumps(tenant[0])) if "account" in args else completed(args)

    runner = RecordingRunner(resolve)
    adapter = configured_adapter(runner, tenant=TENANT_ID)
    adapter.investigate("target-alpha", "delegated_permissions", "P7D")
    previous_calls = len(runner.calls)
    tenant[0] = CUSTOMER_ID

    with pytest.raises(azure_cli().WorkspaceConfigurationError):
        adapter.investigate("target-alpha", "delegated_permissions", "P7D")

    assert len(runner.calls) == previous_calls + 1
    assert runner.calls[-1][0][:3] == ("az-test", "account", "show")


@pytest.mark.parametrize("stage", ("tenant", "workspace"))
@pytest.mark.parametrize(
    "failure,status,reason",
    (
        ("timeout", "failed", "timeout"),
        ("output_limit", "failed", "output_limit"),
        ("missing_cli", "unavailable", "cli_unavailable"),
        ("unicode", "failed", "invalid_response"),
        ("recursion", "failed", "invalid_response"),
        ("malformed_json", "failed", "invalid_response"),
        ("object", "failed", "invalid_response"),
        ("null", "failed", "invalid_response"),
        ("empty", "failed", "invalid_response"),
        ("invalid_guid", "failed", "invalid_response"),
        ("access_denied", "failed", "access_denied"),
        ("nonzero", "failed", "query_failed"),
        ("oversize_stdout", "failed", "output_limit"),
        ("oversize_stderr", "failed", "output_limit"),
    ),
)
def test_metadata_failures_stop_queries_and_keep_precise_sanitized_reasons(stage, failure, status, reason):
    marker = "opaque-account-metadata-must-not-escape"

    def fail(args, kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(marker, kwargs["timeout"])
        if failure == "output_limit":
            raise azure_cli().OutputLimitExceeded(marker)
        if failure == "missing_cli":
            raise OSError(marker)
        if failure == "unicode":
            return completed(args, stdout=b"\xff")
        if failure == "recursion":
            raise RecursionError(marker)
        if failure == "oversize_stdout":
            return completed(args, stdout="x" * (azure_cli().MAX_OUTPUT_BYTES + 1))
        if failure == "oversize_stderr":
            return completed(args, stderr="x" * (azure_cli().MAX_OUTPUT_BYTES + 1))
        if failure in {"access_denied", "nonzero"}:
            return completed(
                args, code=1,
                stderr=("Forbidden: " if failure == "access_denied" else "") + marker,
            )
        output = {
            "malformed_json": "{broken",
            "object": json.dumps({"tenantId": TENANT_ID, "account": marker}),
            "null": "null",
            "empty": '""',
            "invalid_guid": json.dumps(marker),
        }
        return completed(args, stdout=output[failure])

    runner = RecordingRunner(fail)
    adapter = configured_adapter(
        runner, customer=None, tenant=TENANT_ID if stage == "tenant" else None
    )

    outcomes = adapter.investigate("target-alpha", "delegated_permissions", "P7D")

    assert len(runner.calls) == 1
    assert outcomes
    assert all(outcome.status == status for outcome in outcomes)
    assert all(outcome.reason_codes == (reason,) for outcome in outcomes)
    assert marker not in repr([outcome.public() for outcome in outcomes])
    assert TENANT_ID not in repr([outcome.public() for outcome in outcomes])


def test_preflight_and_queries_share_the_total_investigation_budget(monkeypatch):
    import trackback.adapter as implementation

    now = [100.0]
    monkeypatch.setattr(implementation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(implementation, "INVESTIGATION_TIMEOUT", 1)

    def resolve(args, kwargs):
        assert kwargs["timeout"] <= 101.0 - now[0]
        now[0] += 0.4
        if "account" in args:
            return completed(args, stdout=json.dumps(TENANT_ID))
        if "show" in args:
            return completed(args, stdout=json.dumps(CUSTOMER_ID))
        return completed(args)

    runner = RecordingRunner(resolve)
    adapter = configured_adapter(runner, customer=None, tenant=TENANT_ID)

    outcomes = adapter.investigate("target-alpha", "delegated_permissions", "P7D")

    assert len(runner.calls) == 3
    assert runner.calls[0][1]["timeout"] == 1
    assert runner.calls[1][1]["timeout"] == pytest.approx(0.6)
    assert runner.calls[2][1]["timeout"] == pytest.approx(0.2)
    assert outcomes[0].status == "succeeded"
    assert all(outcome.reason_codes == ("budget_exhausted",) for outcome in outcomes[1:])


def test_exhausted_tenant_preflight_budget_prevents_workspace_lookup(monkeypatch):
    import trackback.adapter as implementation

    now = [100.0]
    monkeypatch.setattr(implementation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(implementation, "INVESTIGATION_TIMEOUT", 1)

    def expire(args, kwargs):
        now[0] += 1
        return completed(args, stdout=json.dumps(TENANT_ID))

    runner = RecordingRunner(expire)

    outcomes = configured_adapter(runner, customer=None, tenant=TENANT_ID).investigate(
        "target-alpha", "delegated_permissions", "P7D"
    )

    assert len(runner.calls) == 1
    assert all(outcome.reason_codes == ("budget_exhausted",) for outcome in outcomes)


def test_invalid_environment_is_not_ready_and_never_executes_cli(workspace_environment):
    runner = RecordingRunner(lambda args, kwargs: completed(args))
    adapter = azure_cli().AzureCLIAdapter(runner=runner, command=["az-test"])

    assert adapter.ready is False
    with pytest.raises(azure_cli().WorkspaceConfigurationError) as caught:
        adapter.investigate("target-alpha", "delegated_permissions", "P7D")

    assert caught.value.reason == "missing_workspace_identity"
    assert runner.calls == []


def test_injected_workspace_is_independent_of_environment(workspace_environment):
    workspace_environment({"AZURE_TENANT_ID": "invalid-tenant-setting"})
    runner = RecordingRunner(lambda args, kwargs: completed(args))
    adapter = adapter_for(runner)

    assert adapter.ready
    outcomes = adapter.investigate("target-alpha", "delegated_permissions", "P7D")

    assert all(outcome.status == "succeeded" for outcome in outcomes)
    assert not any("account" in args for args, _ in runner.calls)


def test_live_investigation_never_queries_self_reported_application_logs():
    from trackback.queries import QUERY_PLANS

    assert [p.source for p in QUERY_PLANS.values() if not p.native] == ["ContainerAppConsoleLogs"]
