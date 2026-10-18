from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def workspace_environment(monkeypatch):
    names = (
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
        "LOG_ANALYTICS_RESOURCE_GROUP",
        "LOG_ANALYTICS_WORKSPACE_NAME",
        "LOG_ANALYTICS_WORKSPACE_RESOURCE_ID",
        "LOG_ANALYTICS_WORKSPACE_ID",
    )

    def configure(values):
        for name in names:
            monkeypatch.delenv(name, raising=False)
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    configure({})
    return configure
