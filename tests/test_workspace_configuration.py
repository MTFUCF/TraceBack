import itertools

import pytest

from trackback.azure_cli import WorkspaceConfig, WorkspaceConfigurationError


SUBSCRIPTION = "abcdefab-2222-4222-8222-222222222222"
CUSTOMER = "abcdefab-1111-4111-8111-111111111111"
TENANT = "abcdefab-3333-4333-8333-333333333333"
RESOURCE = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-portable/"
    "providers/Microsoft.OperationalInsights/workspaces/ws-portable"
)
COMPONENTS = {
    "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
    "LOG_ANALYTICS_RESOURCE_GROUP": "rg-portable",
    "LOG_ANALYTICS_WORKSPACE_NAME": "ws-portable",
}
VALID_SETTINGS = {
    **COMPONENTS,
    "LOG_ANALYTICS_WORKSPACE_RESOURCE_ID": RESOURCE,
    "LOG_ANALYTICS_WORKSPACE_ID": CUSTOMER,
    "AZURE_TENANT_ID": TENANT,
}


@pytest.mark.parametrize("use_resource", (False, True))
@pytest.mark.parametrize("customer", (None, CUSTOMER, CUSTOMER.upper()))
@pytest.mark.parametrize("tenant", (None, TENANT, TENANT.upper()))
def test_both_identity_forms_preserve_independent_optional_ids(
    workspace_environment, use_resource, customer, tenant
):
    values = (
        {"LOG_ANALYTICS_WORKSPACE_RESOURCE_ID": RESOURCE}
        if use_resource else dict(COMPONENTS)
    )
    if customer is not None:
        values["LOG_ANALYTICS_WORKSPACE_ID"] = customer
    if tenant is not None:
        values["AZURE_TENANT_ID"] = tenant
    workspace_environment(values)

    config = WorkspaceConfig.from_environment()

    assert config.resource_id == RESOURCE
    assert config.subscription_id == SUBSCRIPTION
    assert config.customer_id == customer
    assert config.tenant_id == tenant


@pytest.mark.parametrize(
    "names",
    [
        names for count in range(4)
        for names in itertools.combinations(COMPONENTS, count)
    ],
)
@pytest.mark.parametrize("uppercase", (False, True))
def test_full_id_validates_every_supplied_component_without_overriding_it(
    workspace_environment, names, uppercase
):
    values = {
        name: COMPONENTS[name].upper() if uppercase else COMPONENTS[name]
        for name in names
    }
    values["LOG_ANALYTICS_WORKSPACE_RESOURCE_ID"] = RESOURCE
    workspace_environment(values)

    assert WorkspaceConfig.from_environment().resource_id == RESOURCE


@pytest.mark.parametrize(
    "name,value",
    (
        ("AZURE_SUBSCRIPTION_ID", TENANT),
        ("LOG_ANALYTICS_RESOURCE_GROUP", "rg-other"),
        ("LOG_ANALYTICS_WORKSPACE_NAME", "ws-other"),
    ),
)
@pytest.mark.parametrize("all_components", (False, True))
def test_conflicting_complete_or_partial_components_fail_closed(
    workspace_environment, name, value, all_components
):
    values = dict(COMPONENTS) if all_components else {}
    values.update({"LOG_ANALYTICS_WORKSPACE_RESOURCE_ID": RESOURCE, name: value})
    workspace_environment(values)

    with pytest.raises(WorkspaceConfigurationError) as caught:
        WorkspaceConfig.from_environment()

    assert caught.value.reason == "workspace_identity_mismatch"
    assert RESOURCE not in str(caught.value)
    assert value not in str(caught.value)


@pytest.mark.parametrize(
    "names",
    [
        names for count in range(3)
        for names in itertools.combinations(COMPONENTS, count)
    ],
)
@pytest.mark.parametrize("with_customer", (False, True))
def test_all_incomplete_identities_fail_even_with_customer_id(
    workspace_environment, names, with_customer
):
    values = {name: COMPONENTS[name] for name in names}
    if with_customer:
        values["LOG_ANALYTICS_WORKSPACE_ID"] = CUSTOMER
    workspace_environment(values)

    with pytest.raises(WorkspaceConfigurationError) as caught:
        WorkspaceConfig.from_environment()

    assert caught.value.reason == "missing_workspace_identity"
    for name in ("LOG_ANALYTICS_WORKSPACE_RESOURCE_ID", *COMPONENTS):
        assert name in str(caught.value)


@pytest.mark.parametrize("name", VALID_SETTINGS)
@pytest.mark.parametrize("value", ("", " ", "\t", "\n", "unsafe;opaque-config-marker"))
def test_supplied_empty_or_malformed_settings_are_not_ignored(
    workspace_environment, name, value
):
    values = {**VALID_SETTINGS, name: value}
    workspace_environment(values)

    with pytest.raises(WorkspaceConfigurationError) as caught:
        WorkspaceConfig.from_environment()

    assert caught.value.reason.startswith("invalid_")
    assert "opaque-config-marker" not in str(caught.value.public())


@pytest.mark.parametrize("name", VALID_SETTINGS)
@pytest.mark.parametrize("padding", (" ", "\n", "\t"))
def test_valid_settings_with_padding_are_rejected_not_trimmed(
    workspace_environment, name, padding
):
    workspace_environment({**VALID_SETTINGS, name: VALID_SETTINGS[name] + padding})

    with pytest.raises(WorkspaceConfigurationError):
        WorkspaceConfig.from_environment()


@pytest.mark.parametrize(
    "resource",
    (
        None, 42, "", CUSTOMER,
        RESOURCE + "/",
        RESOURCE + "/tables/SigninLogs",
        RESOURCE + "?api-version=1",
        RESOURCE + "#fragment",
        "https://management.azure.com" + RESOURCE,
        RESOURCE.replace("/subscriptions/", "subscriptions/"),
        RESOURCE.replace("/resourceGroups/", "//resourceGroups/"),
        RESOURCE.replace(SUBSCRIPTION, "subscription-name"),
        RESOURCE.replace("Microsoft.OperationalInsights", "Microsoft.Other"),
        RESOURCE.replace("rg-portable", "rg-portable."),
        RESOURCE.replace("rg-portable", "../rg-portable"),
        RESOURCE.replace("ws-portable", "-workspace"),
        RESOURCE.replace("ws-portable", "workspace-"),
        RESOURCE.replace("ws-portable", "ws_underscore"),
        RESOURCE.replace("ws-portable", "abc"),
        RESOURCE.replace("ws-portable", "a" * 64),
        RESOURCE.replace("rg-portable", "r" * 91),
    ),
)
def test_direct_configuration_requires_a_well_formed_full_identity(resource):
    with pytest.raises(WorkspaceConfigurationError) as caught:
        WorkspaceConfig(resource)

    assert caught.value.reason == "invalid_workspace_resource_id"


@pytest.mark.parametrize("field", ("customer_id", "tenant_id"))
@pytest.mark.parametrize("value", (42, [], "", "not-a-guid", "{" + CUSTOMER + "}"))
def test_direct_configuration_rejects_invalid_optional_ids(field, value):
    with pytest.raises(WorkspaceConfigurationError):
        WorkspaceConfig(RESOURCE, **{field: value})


@pytest.mark.parametrize("group", ("r", "r" * 90, "rg.with_(parentheses)-9", "--debug"))
@pytest.mark.parametrize("name", ("w123", "w" * 63, "Workspace-9"))
def test_supported_name_boundaries_round_trip(workspace_environment, group, name):
    workspace_environment({
        **COMPONENTS,
        "LOG_ANALYTICS_RESOURCE_GROUP": group,
        "LOG_ANALYTICS_WORKSPACE_NAME": name,
    })

    config = WorkspaceConfig.from_environment()

    assert config.resource_id == (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{group}/"
        f"providers/Microsoft.OperationalInsights/workspaces/{name}"
    )


def test_full_id_path_labels_are_case_insensitive(workspace_environment):
    workspace_environment({"LOG_ANALYTICS_WORKSPACE_RESOURCE_ID": RESOURCE.upper()})

    assert WorkspaceConfig.from_environment().resource_id == RESOURCE.upper()


def test_invalid_full_id_never_falls_back_to_valid_components(workspace_environment):
    workspace_environment({**COMPONENTS, "LOG_ANALYTICS_WORKSPACE_RESOURCE_ID": ""})

    with pytest.raises(WorkspaceConfigurationError) as caught:
        WorkspaceConfig.from_environment()

    assert caught.value.reason == "invalid_workspace_resource_id"
