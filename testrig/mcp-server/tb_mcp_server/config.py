from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from urllib.parse import urlsplit


class AuthMode(StrEnum):
    DELEGATED = "delegated"
    APPLICATION = "application"


class ConfigurationError(ValueError):
    """Sanitized configuration error with stable reason codes."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)

    def public(self) -> dict[str, str]:
        return {"error": "invalid_mcp_server_configuration", "reason": self.reason, "message": str(self)}


@dataclass(frozen=True)
class ServerConfig:
    auth_mode: AuthMode
    graph_scopes: tuple[str, ...]
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    managed_identity_client_id: str | None = None
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    platform_id_headers: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> "ServerConfig":
        env = os.environ if environ is None else environ
        auth_mode = _required(env, "TB_AUTH_MODE")
        try:
            mode = AuthMode(auth_mode)
        except ValueError as exc:
            raise ConfigurationError("invalid_auth_mode", "TB_AUTH_MODE must be delegated or application.") from exc

        graph_scopes = _split_csv(_required(env, "TB_GRAPH_SCOPES"))
        base_url = _optional(env, "TB_GRAPH_BASE_URL") or "https://graph.microsoft.com/v1.0"
        _validate_https_base_url(base_url)
        tenant_id = _optional(env, "TB_TENANT_ID")
        client_id = _optional(env, "TB_CLIENT_ID")
        client_secret = _optional(env, "TB_CLIENT_SECRET")
        mi_client_id = _optional(env, "TB_MANAGED_IDENTITY_CLIENT_ID")
        platform_headers = tuple(name.lower() for name in _split_csv(_optional(env, "TB_PLATFORM_ID_HEADERS") or ""))

        if mode is AuthMode.DELEGATED:
            _require_present("TB_TENANT_ID", tenant_id)
            _require_present("TB_CLIENT_ID", client_id)
        elif client_secret is not None:
            _require_present("TB_TENANT_ID", tenant_id)
            _require_present("TB_CLIENT_ID", client_id)
        return cls(
            auth_mode=mode,
            graph_scopes=tuple(graph_scopes),
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            managed_identity_client_id=mi_client_id,
            graph_base_url=base_url.rstrip("/"),
            platform_id_headers=platform_headers,
        )


def _required(env: dict[str, str], name: str) -> str:
    value = _optional(env, name)
    _require_present(name, value)
    return value


def _optional(env: dict[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() != value or value == "":
        raise ConfigurationError(f"invalid_{name.lower()}", f"{name} must be non-empty and must not include padding.")
    return value


def _require_present(name: str, value: str | None) -> None:
    if value is None:
        raise ConfigurationError(f"missing_{name.lower()}", f"Set {name}.")


def _split_csv(value: str) -> list[str]:
    if value == "":
        return []
    parts = value.split(",")
    if any(part.strip() != part or part == "" for part in parts):
        raise ConfigurationError("invalid_csv_setting", "Comma-separated settings must contain non-empty unpadded values.")
    return parts


def _validate_https_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ConfigurationError("invalid_graph_base_url", "TB_GRAPH_BASE_URL must be an HTTPS origin/path without query or fragment.")
