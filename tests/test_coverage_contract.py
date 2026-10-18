import json
from types import SimpleNamespace

from trackback.adapter import AzureCLIAdapter, InMemoryAdapter, WorkspaceConfig
from trackback.coverage import CoverageChecker

WORKSPACE = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg"
    "/providers/Microsoft.OperationalInsights/workspaces/workspace-alpha"
)
DCR = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Insights/dataCollectionRules/dcr"
ALL_CATEGORIES = (
    "MicrosoftGraphActivityLogs", "SignInLogs", "NonInteractiveUserSignInLogs",
    "ServicePrincipalSignInLogs", "ManagedIdentitySignInLogs", "AuditLogs",
)


def _table(columns, rows):
    return {"tables": [{"columns": [{"name": c} for c in columns], "rows": rows}]}


def _runner(*, quota=-1, transform=None, categories=ALL_CATEGORIES, tables=None, methods=None):
    tables = tables if tables is not None else {
        "MicrosoftGraphActivityLogs": 5, "AADNonInteractiveUserSignInLogs": 5, "SigninLogs": 5,
        "AADManagedIdentitySignInLogs": 5, "AADServicePrincipalSignInLogs": 5,
    }
    methods = methods if methods is not None else [["GET", 10, 4], ["POST", 3, 0]]
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        joined = " ".join(args)
        if "aadiam/diagnosticSettings" in joined:
            body = {"value": [{"properties": {
                "workspaceId": WORKSPACE.upper(),
                "logs": [{"category": c, "enabled": True} for c in categories],
            }}]}
        elif "dataCollectionRules" in joined:
            body = {"properties": {"dataFlows": [{
                "streams": ["Microsoft-Table-MicrosoftGraphActivityLogs"], "transformKql": transform,
            }]}}
        elif "workspaces/workspace-alpha" in joined:
            body = {"properties": {
                "customerId": "00000000-0000-0000-0000-0000000000aa",
                "workspaceCapping": {"dailyQuotaGb": quota, "dataIngestionStatus": "RespectQuota"},
                "defaultDataCollectionRuleResourceId": DCR if transform else None,
            }}
        elif "summarize Rows = count() by SourceTable" in joined:
            body = _table(["SourceTable", "Rows"], [[k, v] for k, v in tables.items()])
        elif "UserReads" in joined:
            body = _table(["RequestMethod", "Rows", "UserReads"], methods)
        else:
            raise AssertionError(joined)
        return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")

    return run, calls


def _checker(runner):
    return CoverageChecker(AzureCLIAdapter(runner=runner, workspace=WorkspaceConfig(WORKSPACE), command=["az"]))


def _by_id(result):
    return {check["id"]: check for check in result["checks"]}


def test_healthy_workspace_reports_ok_and_never_echoes_kql_or_ids():
    runner, _ = _runner()
    result = _checker(runner).run()

    assert result["status"] == "degraded"  # supporting App Insights tables are silent
    checks = _by_id(result)
    assert checks["daily_cap"]["reason_code"] == "no_daily_cap"
    assert checks["workspace_transforms"]["reason_code"] == "no_workspace_transform"
    assert checks["entra_diagnostic_settings"]["status"] == "ok"
    assert checks["table_ingestion"]["detail"]["silent_core"] == []
    assert checks["graph_read_coverage"]["reason_code"] == "user_reads_present"
    assert "0000000000aa" not in json.dumps(result)


def test_transform_that_drops_user_reads_is_detected_from_config_and_data():
    runner, _ = _runner(
        quota=0.25,
        transform="source | where RequestMethod in ('POST','PATCH','DELETE')",
        methods=[["GET", 50, 0], ["POST", 9, 0]],
    )
    checks = _by_id(_checker(runner).run())

    assert checks["daily_cap"] == {
        "id": "daily_cap", "status": "warn", "reason_code": "daily_cap_configured",
        "detail": {"daily_quota_gb": 0.25},
    }
    assert checks["workspace_transforms"]["reason_code"] == "evidence_rows_filtered"
    assert checks["workspace_transforms"]["detail"] == {"tables": ["MicrosoftGraphActivityLogs"]}
    assert "transformKql" not in json.dumps(checks)
    assert checks["graph_read_coverage"]["status"] == "fail"
    assert checks["graph_read_coverage"]["reason_code"] == "user_reads_missing"


def test_missing_entra_categories_and_silent_core_tables_fail():
    runner, _ = _runner(categories=("AuditLogs",), tables={"SigninLogs": 1})
    result = _checker(runner).run()
    checks = _by_id(result)

    assert result["status"] == "failed"
    assert checks["entra_diagnostic_settings"]["detail"]["missing"] == [
        "MicrosoftGraphActivityLogs", "SignInLogs", "NonInteractiveUserSignInLogs",
        "ServicePrincipalSignInLogs", "ManagedIdentitySignInLogs",
    ]
    assert "MicrosoftGraphActivityLogs" in checks["table_ingestion"]["detail"]["silent_core"]


def test_access_denied_is_reported_as_unknown_not_ok():
    def denied(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="AuthorizationFailed")

    result = _checker(denied).run()

    assert {c["status"] for c in result["checks"]} == {"unknown"}
    assert result["status"] == "unknown"


def test_coverage_endpoint_is_local_and_safe_with_fixture_adapter():
    from app import create_app

    client = create_app(adapter=InMemoryAdapter(), config={"TESTING": True}).test_client()
    response = client.get("/api/coverage")

    assert response.status_code == 200
    assert response.get_json()["checks"][0]["reason_code"] == "coverage_requires_azure_cli_adapter"
