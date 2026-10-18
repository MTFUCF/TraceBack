from dataclasses import replace

from tests.contract_support import (
    application_chain,
    contracts,
    correlation,
    delegated_chain,
    outcomes,
    record,
)


def correlate(records, mode, *, failures=()):
    function = getattr(correlation(), "correlate", correlation().correlate_chain)
    return function(outcomes(records, failures=failures), mode)


def test_delegated_permissions_produce_complete_seven_hop_chain():
    result = correlate(delegated_chain(), "delegated_permissions")

    assert result["status"] == "complete"
    assert result["human_attribution"] == "supported"
    assert [hop["id"] for hop in result["hops"]] == [f"H{i}" for i in range(1, 8)]
    assert [(link["source"], link["target"]) for link in result["links"]] == [
        (f"H{i}", f"H{i + 1}") for i in range(1, 7)
    ]
    assert all(link["status"] == "linked" for link in result["links"])
    assert all(link["join_type"] == "exact_identifier" for link in result["links"])


def test_application_permissions_require_and_preserve_backward_human_chain():
    result = correlate(application_chain(), "application_permissions")

    assert result["status"] == "complete"
    assert result["human_attribution"] == "supported"
    assert "backward_human_link_required" not in result["reason_codes"]
    workload_to_action = result["links"][4]
    assert workload_to_action["status"] == "linked"
    assert "token_id" in workload_to_action["key_types"]


def test_application_action_can_be_structurally_impossible_to_attribute_to_human():
    records = (
        record(
            "H1",
            "SigninLogs",
            ids={"user_id": "human-only"},
            evidence_type="sign_in",
            actor_type="user",
            action_category="sign_in",
            permission_mode="delegated_permissions",
        ),
        record("H2", "CopilotActivity", ids={"agent_id": "agent-only"}, second=1, actor_type="agent"),
        record(
            "H3",
            "AppRequests",
            ids={"trace_id": "foundry-only"},
            second=2,
            evidence_type="service_request",
            actor_type="agent",
        ),
        record(
            "H4",
            "AppDependencies",
            ids={"request_id": "tool-only"},
            second=3,
            evidence_type="service_request",
        ),
        record(
            "H5",
            "AADServicePrincipalSignInLogs",
            ids={"token_id": "workload-token", "service_principal_id": "spn-only"},
            second=4,
            evidence_type="sign_in",
            actor_type="service_principal",
            permission_mode="application_permissions",
        ),
        record(
            "H6",
            "MicrosoftGraphActivityLogs",
            ids={"token_id": "workload-token", "service_principal_id": "spn-only"},
            second=5,
            evidence_type="api_action",
            actor_type="service_principal",
            permission_mode="application_permissions",
        ),
        record(
            "H7",
            "MicrosoftGraphActivityLogs",
            ids={"token_id": "workload-token", "service_principal_id": "spn-only"},
            second=5,
            evidence_type="target",
            actor_type="service_principal",
            permission_mode="application_permissions",
        ),
    )

    result = correlate(records, "application_permissions")

    assert result["status"] == "impossible"
    assert result["human_attribution"] == "unresolved"
    assert "structurally_unlinkable" in result["reason_codes"]
    assert "backward_human_link_required" in result["reason_codes"]


def test_missing_mcp_telemetry_is_an_evidence_gap_not_a_manufactured_hop():
    records = tuple(item for item in delegated_chain() if item.hop != "H4")

    result = correlate(records, "delegated_permissions")

    assert result["status"] == "partial"
    assert result["hops"][3]["status"] == "unavailable"
    assert result["links"][2]["status"] == "unavailable"
    assert result["links"][3]["status"] == "unavailable"
    assert all(
        (link["source"], link["target"]) != ("H3", "H5")
        for link in result["links"]
    )
    assert result["human_attribution"] == "unresolved"


def test_ambiguous_human_candidates_remain_ambiguous_regardless_of_row_order():
    chain = list(delegated_chain())
    second_human = replace(
        chain[0],
        ids={
            "request_id": ("req-human",),
            "user_id": ("user-competing",),
        },
    )

    forward = correlate((chain[0], second_human, *chain[1:]), "delegated_permissions")
    reverse = correlate((second_human, chain[0], *chain[1:]), "delegated_permissions")

    for result in (forward, reverse):
        assert result["status"] == "unresolved"
        assert result["links"][0]["status"] == "unresolved"
        assert result["links"][0]["confidence"] == "none"
        assert result["links"][0]["reason_codes"] == ["ambiguous"]


def test_duplicate_evidence_does_not_inflate_link_or_overall_confidence():
    chain = delegated_chain()
    contract = contracts()
    base_outcomes = outcomes(chain)
    duplicated = (
        *base_outcomes,
        contract.QueryOutcome(
            chain[0].source,
            "succeeded",
            (chain[0], chain[1], chain[0], chain[1]),
        ),
    )
    function = getattr(correlation(), "correlate", correlation().correlate_chain)

    baseline = function(base_outcomes, "delegated_permissions")
    repeated = function(duplicated, "delegated_permissions")

    assert repeated["status"] == baseline["status"]
    assert repeated["confidence"] == baseline["confidence"]
    assert [
        (link["status"], link["confidence"], link["key_types"])
        for link in repeated["links"]
    ] == [
        (link["status"], link["confidence"], link["key_types"])
        for link in baseline["links"]
    ]


def test_overall_confidence_is_the_weakest_required_link():
    chain = list(delegated_chain())
    chain[1] = replace(
        chain[1],
        ids={
            "request_id": ("req-human",),
            "app_id": ("app-stable",),
            "agent_id": ("agent-stable",),
            "resource_id": ("resource-stable",),
        },
    )
    chain[2] = replace(
        chain[2],
        ids={
            "app_id": ("app-stable",),
            "agent_id": ("agent-stable",),
            "resource_id": ("resource-stable",),
            "span_id": ("span-foundry",),
        },
    )

    result = correlate(chain, "delegated_permissions")

    link_confidences = [link["confidence"] for link in result["links"]]
    assert "medium" in link_confidences
    assert result["confidence"] == "medium"
    assert result["links"][1]["join_type"] == "heuristic_time_window"


def test_time_and_identity_only_h4_to_h5_link_stays_heuristic_candidate():
    records = (
        record(
            "H4",
            "ContainerAppConsoleLogs",
            ids={"app_id": "app-alpha"},
            evidence_type="service_request",
            actor_type="service_principal",
            action_category="execute",
            permission_mode="application_permissions",
        ),
        record(
            "H5",
            "AADServicePrincipalSignInLogs",
            ids={"app_id": "app-alpha"},
            second=20,
            evidence_type="sign_in",
            actor_type="service_principal",
            action_category="sign_in",
            permission_mode="application_permissions",
        ),
    )

    result = correlate(records, "application_permissions")
    link = result["links"][3]

    assert link["source"] == "H4"
    assert link["target"] == "H5"
    assert link["status"] == "candidate"
    assert link["confidence"] == "low"
    assert link["join_type"] == "heuristic_time_window"
    assert link["reason_codes"] == ["candidate_only"]


def test_only_adjacent_links_are_emitted_and_each_has_evidence_provenance():
    result = correlate(delegated_chain(), "delegated_permissions")

    assert len(result["links"]) == 6
    for index, link in enumerate(result["links"], start=1):
        assert (link["source"], link["target"]) == (f"H{index}", f"H{index + 1}")
        assert len(link["evidence_refs"]) == 2
        assert all(ref.startswith("evidence-") for ref in link["evidence_refs"])
        assert link["key_types"]


def _graph_only(mode, ids, second=5):
    return record(
        "H6", "MicrosoftGraphActivityLogs", ids=ids, second=second, evidence_type="api_action",
        actor_type="user" if mode == "delegated_permissions" else "service_principal",
        action_category="read", resource_type="user", permission_mode=mode,
    )


def test_last_hop_names_delegated_user_with_exact_token_binding_even_when_chain_is_partial():
    chain = delegated_chain()
    result = correlate((chain[4], chain[5]), "delegated_permissions")

    assert result["human_attribution"] == "unresolved"
    last = result["last_hop"]
    assert last["verdict"] == "user_identified"
    assert last["strength"] == "token_exact"
    assert last["human"] == "in_api_telemetry"
    [action] = last["actions"]
    assert action["binding"] == "token_exact"
    assert action["actor_ref"].startswith("corr-") and action["sign_in_ref"].startswith("evidence-")
    assert "user-alpha" not in str(result)


def test_last_hop_without_sign_in_is_api_log_only_and_never_timing_based():
    other_token_sign_in = record(
        "H5", "AADNonInteractiveUserSignInLogs", second=5,
        ids={"token_id": "token-other", "user_id": "user-alpha"},
        evidence_type="sign_in", actor_type="user", action_category="sign_in",
        permission_mode="delegated_permissions",
    )
    action = _graph_only("delegated_permissions", {"token_id": "token-delegated", "user_id": "user-alpha"})
    last = correlate((other_token_sign_in, action), "delegated_permissions")["last_hop"]

    assert last["verdict"] == "user_identified"
    assert last["strength"] == "api_log_only"
    assert last["actions"][0]["sign_in_ref"] is None


def test_last_hop_flags_token_bound_to_a_different_user():
    sign_in = record(
        "H5", "AADNonInteractiveUserSignInLogs", second=4,
        ids={"token_id": "token-delegated", "user_id": "user-gamma"},
        evidence_type="sign_in", actor_type="user", action_category="sign_in",
        permission_mode="delegated_permissions",
    )
    action = _graph_only("delegated_permissions", {"token_id": "token-delegated", "user_id": "user-alpha"})
    last = correlate((sign_in, action), "delegated_permissions")["last_hop"]

    assert last["verdict"] == "unresolved"
    assert "contradictory_ids" in last["reason_codes"]


def test_last_hop_reports_multiple_users_separately():
    first = _graph_only("delegated_permissions", {"token_id": "t1", "user_id": "user-alpha"}, 5)
    second = _graph_only("delegated_permissions", {"token_id": "t2", "user_id": "user-gamma"}, 6)
    last = correlate((first, second), "delegated_permissions")["last_hop"]

    assert last["verdict"] == "multiple_actors"
    assert len({a["actor_ref"] for a in last["actions"]}) == 2


def test_last_hop_application_identifies_workload_but_not_human():
    chain = application_chain()
    last = correlate((chain[4], chain[5]), "application_permissions")["last_hop"]

    assert last["verdict"] == "workload_identified"
    assert last["strength"] == "token_exact"
    assert last["human"] == "not_in_api_telemetry"
    assert last["actions"][0]["actor_type"] == "workload"
    assert last["actions"][0]["client_app_ref"].startswith("corr-")


def test_last_hop_ignores_other_permission_mode_and_reports_no_evidence():
    chain = application_chain()
    last = correlate((chain[4], chain[5]), "delegated_permissions")["last_hop"]

    assert last["verdict"] == "no_evidence"
    assert set(last["reason_codes"]) == {"evidence_gap", "permission_mode_mismatch"}
