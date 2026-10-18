from importlib import import_module, util

import pytest


@pytest.mark.parametrize(
    "name",
    (
        "trackback.contracts",
        "trackback.telemetry",
        "trackback.azure_cli",
        "trackback.correlation",
        "trackback.service",
    ),
)
def test_required_trackback_modules_exist(name):
    assert util.find_spec(name) is not None, f"missing agreed module: {name}"
    import_module(name)


def test_package_is_trackback_not_traceback():
    import trackback

    assert trackback.__name__ == "trackback"
    with pytest.raises(ModuleNotFoundError):
        import_module("traceback.contracts")


def test_contract_module_exports_public_types():
    try:
        contracts = import_module("trackback.contracts")
    except ModuleNotFoundError:
        pytest.fail("trackback.contracts must own the public contract types")

    for symbol in ("InvestigationRequest", "EvidenceRecord", "QueryOutcome", "HOPS"):
        assert hasattr(contracts, symbol), f"trackback.contracts missing {symbol}"
