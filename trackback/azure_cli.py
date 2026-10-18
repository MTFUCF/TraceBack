"""Public bounded Azure CLI adapter surface."""

from .adapter import (
    INVESTIGATION_TIMEOUT,
    MAX_OUTPUT_BYTES,
    QUERY_TIMEOUT,
    AzureCLIAdapter,
    OutputLimitExceeded,
    WorkspaceConfig,
    WorkspaceConfigurationError,
    bounded_runner,
    classify_failure,
    cli_command,
)

__all__ = [
    "AzureCLIAdapter",
    "INVESTIGATION_TIMEOUT",
    "MAX_OUTPUT_BYTES",
    "OutputLimitExceeded",
    "QUERY_TIMEOUT",
    "WorkspaceConfig",
    "WorkspaceConfigurationError",
    "bounded_runner",
    "classify_failure",
    "cli_command",
]
