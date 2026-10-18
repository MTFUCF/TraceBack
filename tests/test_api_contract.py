import json

import pytest

from app import create_app
from tests.contract_support import (
    StaticAdapter,
    application_chain,
    contracts,
    delegated_chain,
    outcomes,
)


@pytest.fixture
def app_factory():
    def factory(query_outcomes):
        app = create_app(
            adapter=StaticAdapter(query_outcomes),
            config={
                "TESTING": True,
                "INVESTIGATION_RATE_LIMIT": 100,
                "PROPAGATE_EXCEPTIONS": False,
            },
        )
        return app

    return factory


def post(client, *, target="target-alpha", mode="delegated_permissions", timespan="P30D"):
    return client.post(
        "/api/investigate",
        json={"target": target, "mode": mode, "timespan": timespan},
    )


def test_delegated_api_contract_has_fixed_bounded_public_schema(app_factory):
    app = app_factory(outcomes(delegated_chain()))

    response = post(app.test_client())
    body = response.get_json()

    assert response.status_code == 200
    assert set(body) == {
        "confidence",
        "hops",
        "human_attribution",
        "last_hop",
        "links",
        "mode",
        "query_outcomes",
        "reason_codes",
        "status",
        "timespan",
    }
    assert len(body["hops"]) == 7
    assert len(body["links"]) == 6
    assert len(response.data) < 64 * 1024
    assert body["status"] == "complete"


def test_application_chain_is_supported_only_when_backward_human_links_exist(app_factory):
    app = app_factory(outcomes(application_chain()))

    response = post(app.test_client(), mode="application_permissions")
    body = response.get_json()

    assert response.status_code == 200
    assert body["status"] == "complete"
    assert body["human_attribution"] == "supported"
    assert "backward_human_link_required" not in body["reason_codes"]


def test_all_sources_unavailable_returns_503_and_never_claims_impossibility(app_factory):
    contract = contracts()
    query_outcomes = (
        contract.QueryOutcome(
            "MicrosoftGraphActivityLogs",
            "unavailable",
            reason_codes=("source_unavailable",),
        ),
        contract.QueryOutcome(
            "CopilotActivity",
            "unavailable",
            reason_codes=("source_unavailable",),
        ),
    )
    app = app_factory(query_outcomes)

    response = post(app.test_client())
    body = response.get_json()

    assert response.status_code == 503
    assert body["status"] == "unavailable"
    assert body["human_attribution"] == "unresolved"
    assert "no_required_source" in body["reason_codes"]
    assert "structurally_unlinkable" not in body["reason_codes"]


def test_partial_source_failure_is_visible_in_api_response(app_factory):
    query_outcomes = outcomes(
        delegated_chain(),
        failures=(("AppExceptions", "failed", "query_failed", True),),
    )
    app = app_factory(query_outcomes)

    response = post(app.test_client())
    body = response.get_json()

    assert response.status_code == 200
    assert body["status"] == "partial"
    assert "incomplete_coverage" in body["reason_codes"]
    failed = [
        item for item in body["query_outcomes"] if item["source"] == "AppExceptions"
    ]
    assert failed == [
        {
            "source": "AppExceptions",
            "status": "failed",
            "record_count": 0,
            "truncated": False,
            "reason_codes": ["query_failed"],
        }
    ]


@pytest.mark.parametrize(
    "payload",
    (
        None,
        {},
        {"target": "x", "mode": "delegated_permissions"},
        {
            "target": "x",
            "mode": "delegated_permissions",
            "timespan": "P30D",
            "extra": "not-allowed",
        },
        {"target": "x", "mode": "mixed", "timespan": "P30D"},
        {"target": "x", "mode": "delegated_permissions", "timespan": "PT1H"},
        {"target": "x\x00hidden", "mode": "delegated_permissions", "timespan": "P30D"},
        {"target": "x" * 257, "mode": "delegated_permissions", "timespan": "P30D"},
    ),
)
def test_invalid_requests_never_invoke_adapter(app_factory, payload):
    app = app_factory(outcomes(delegated_chain()))
    adapter = app.extensions["trackback_adapter"]
    client = app.test_client()

    if payload is None:
        response = client.post(
            "/api/investigate",
            data="{broken",
            content_type="application/json",
        )
    else:
        response = client.post("/api/investigate", json=payload)

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_request"}
    assert adapter.calls == []


def test_oversized_json_body_is_rejected_before_adapter(app_factory):
    app = app_factory(outcomes(delegated_chain()))
    adapter = app.extensions["trackback_adapter"]
    payload = {
        "target": "safe",
        "mode": "delegated_permissions",
        "timespan": "P30D",
        "padding": "x" * 5000,
    }

    response = app.test_client().post(
        "/api/investigate",
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_request"}
    assert adapter.calls == []


def test_raw_ids_rows_stderr_account_data_and_markup_do_not_reach_api(app_factory):
    marker = '<img src=x onerror="globalThis.pwned=true">'
    chain = list(delegated_chain())
    chain[0] = contracts().EvidenceRecord(
        source=chain[0].source,
        hop=chain[0].hop,
        event_time=chain[0].event_time,
        evidence_type=chain[0].evidence_type,
        actor_type=chain[0].actor_type,
        ids={
            "request_id": marker,
            "user_id": "raw-human-identifier",
        },
        action_category=chain[0].action_category,
        resource_type=chain[0].resource_type,
        permission_mode=chain[0].permission_mode,
        outcome=chain[0].outcome,
    )
    chain[1] = contracts().EvidenceRecord(
        source=chain[1].source,
        hop=chain[1].hop,
        event_time=chain[1].event_time,
        evidence_type=chain[1].evidence_type,
        actor_type=chain[1].actor_type,
        ids={"request_id": marker, "trace_id": "trace-agent"},
        action_category=chain[1].action_category,
        resource_type=chain[1].resource_type,
        permission_mode=chain[1].permission_mode,
        outcome=chain[1].outcome,
    )
    app = app_factory(outcomes(chain))

    response = post(app.test_client(), target=marker)
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    for forbidden in (
        marker,
        "raw-human-identifier",
        "RawData",
        "LLMEventData",
        "Properties",
        "stderr",
        "accessToken",
        "tenantId",
        "subscriptionId",
        "account",
    ):
        assert forbidden not in text


def test_unexpected_adapter_error_is_opaque_in_response_and_logs(app_factory, caplog):
    marker = "raw-target-and-cli-stderr-must-not-be-logged"

    class FailingAdapter:
        ready = True

        def investigate(self, target, mode, timespan):
            raise RuntimeError(marker)

    app = create_app(
        adapter=FailingAdapter(),
        config={"TESTING": True, "PROPAGATE_EXCEPTIONS": False},
    )

    with caplog.at_level("ERROR"):
        response = post(app.test_client(), target=marker)

    body = response.get_json()
    assert response.status_code == 500
    assert set(body) == {"error", "error_id"}
    assert body["error"] == "internal_error"
    assert marker not in response.get_data(as_text=True)
    assert marker not in caplog.text


def test_health_exposes_only_boolean_readiness_and_nonsensitive_label(app_factory):
    app = app_factory(outcomes(delegated_chain()))

    response = app.test_client().get("/api/health")
    body = response.get_json()

    assert response.status_code == 200
    assert body == {
        "ready": True,
        "workspace": {"name": "Configured Sentinel workspace"},
        "local_only": True,
    }
    serialized = json.dumps(body).lower()
    for forbidden in (
        "account",
        "tenant",
        "subscription",
        "customerid",
        "access_token",
        "stderr",
    ):
        assert forbidden not in serialized


def test_browser_template_has_no_dynamic_executable_html_sink(app_factory):
    app = app_factory(outcomes(delegated_chain()))

    response = app.test_client().get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert ".innerHTML" not in html
    assert "insertAdjacentHTML" not in html
    assert "document.write" not in html


@pytest.mark.parametrize(
    "settings,reason",
    (
        ({}, "missing_workspace_identity"),
        ({"LOG_ANALYTICS_WORKSPACE_RESOURCE_ID": "opaque-config-marker"}, "invalid_workspace_resource_id"),
    ),
)
def test_default_app_fails_closed_with_sanitized_configuration_error(
    workspace_environment, monkeypatch, caplog, settings, reason
):
    import trackback.adapter as implementation
    from tests.test_azure_cli_contract import RecordingRunner, completed

    workspace_environment(settings)
    runner = RecordingRunner(lambda args, kwargs: completed(args))
    monkeypatch.setattr(implementation, "bounded_runner", runner)
    monkeypatch.setattr(implementation, "cli_command", lambda: ["az-test"])
    app = create_app(config={"TESTING": True})
    client = app.test_client()

    health = client.get("/api/health")
    response = post(client)

    assert health.get_json() == {
        "ready": False,
        "workspace": {"name": "Configured Sentinel workspace"},
        "local_only": True,
    }
    assert response.status_code == 503
    assert response.get_json() == implementation.WorkspaceConfigurationError(reason).public()
    assert "opaque-config-marker" not in response.get_data(as_text=True)
    assert "opaque-config-marker" not in caplog.text
    assert runner.calls == []


def test_configured_health_is_local_only_and_tenant_mismatch_is_sanitized(
    workspace_environment, monkeypatch, caplog
):
    import trackback.adapter as implementation
    from tests.test_azure_cli_contract import (
        CUSTOMER_ID, RESOURCE_ID, TENANT_ID, RecordingRunner, completed,
    )

    wrong_tenant = "ffffffff-4444-4444-8444-444444444444"
    workspace_environment({
        "LOG_ANALYTICS_WORKSPACE_RESOURCE_ID": RESOURCE_ID,
        "LOG_ANALYTICS_WORKSPACE_ID": CUSTOMER_ID,
        "AZURE_TENANT_ID": TENANT_ID,
    })
    runner = RecordingRunner(
        lambda args, kwargs: completed(args, stdout=json.dumps(wrong_tenant))
    )
    monkeypatch.setattr(implementation, "bounded_runner", runner)
    monkeypatch.setattr(implementation, "cli_command", lambda: ["az-test"])
    app = create_app(config={"TESTING": True})
    client = app.test_client()

    health = client.get("/api/health")

    assert health.get_json() == {
        "ready": True,
        "workspace": {"name": "Configured Sentinel workspace"},
        "local_only": True,
    }
    assert runner.calls == []
    response = post(client)
    assert response.status_code == 503
    assert response.get_json() == implementation.WorkspaceConfigurationError("tenant_mismatch").public()
    assert len(runner.calls) == 1
    for identifier in (
        RESOURCE_ID, CUSTOMER_ID, TENANT_ID, wrong_tenant,
        "22222222-2222-4222-8222-222222222222", "rg-test", "ws-test",
    ):
        assert identifier not in health.get_data(as_text=True)
        assert identifier not in response.get_data(as_text=True)
        assert identifier not in caplog.text
