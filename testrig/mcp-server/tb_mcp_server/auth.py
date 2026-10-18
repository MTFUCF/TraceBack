from __future__ import annotations

from dataclasses import dataclass

from .config import AuthMode, ConfigurationError, ServerConfig


class AuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccessTokenResult:
    token: str
    source: str


class TokenProvider:
    def __init__(self, config: ServerConfig):
        self.config = config

    def get_graph_token(self, incoming_user_token: str | None) -> AccessTokenResult:
        if self.config.auth_mode is AuthMode.DELEGATED:
            return self._delegated_token(incoming_user_token)
        return self._application_token()

    def _delegated_token(self, incoming_user_token: str | None) -> AccessTokenResult:
        if not incoming_user_token:
            raise AuthenticationError("missing_incoming_user_token")
        if not self.config.client_secret:
            return AccessTokenResult(incoming_user_token, "incoming_delegated_token")
        import msal

        app = msal.ConfidentialClientApplication(
            self.config.client_id,
            authority=f"https://login.microsoftonline.com/{self.config.tenant_id}",
            client_credential=self.config.client_secret,
        )
        result = app.acquire_token_on_behalf_of(incoming_user_token, scopes=list(self.config.graph_scopes))
        if "access_token" not in result:
            raise AuthenticationError(str(result.get("error") or "obo_token_exchange_failed"))
        return AccessTokenResult(result["access_token"], "obo")

    def _application_token(self) -> AccessTokenResult:
        if self.config.client_secret:
            from azure.identity import ClientSecretCredential

            credential = ClientSecretCredential(
                tenant_id=self.config.tenant_id,
                client_id=self.config.client_id,
                client_secret=self.config.client_secret,
            )
            return AccessTokenResult(credential.get_token(*self.config.graph_scopes).token, "client_credentials")
        from azure.identity import DefaultAzureCredential

        kwargs = {}
        if self.config.managed_identity_client_id:
            kwargs["managed_identity_client_id"] = self.config.managed_identity_client_id
        credential = DefaultAzureCredential(**kwargs)
        return AccessTokenResult(credential.get_token(*self.config.graph_scopes).token, "default_azure_credential")


def bearer_token_from_header(header_value: str | None) -> str | None:
    if header_value is None:
        return None
    marker = "Bearer "
    if not header_value.startswith(marker):
        raise ConfigurationError("invalid_authorization_header", "Authorization header must use Bearer authentication.")
    token = header_value[len(marker):]
    if not token:
        raise ConfigurationError("invalid_authorization_header", "Authorization header bearer token is empty.")
    return token
