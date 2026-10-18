from __future__ import annotations

import base64
import json
from typing import Any


# uti is the token's unique identifier: it equals UniqueTokenIdentifier in Entra
# sign-in logs and SignInActivityId in MicrosoftGraphActivityLogs.
SAFE_CLAIMS = ("oid", "sub", "appid", "azp", "tid", "scp", "roles", "uti", "idtyp", "aud")


def safe_token_claims(token: str | None) -> dict[str, Any]:
    """Return only non-sensitive identity claims from a JWT without validating it.

    Validation belongs to the token exchange / Graph call. This helper is only for
    evidence logging and intentionally discards every claim outside SAFE_CLAIMS.
    """
    if not token or token.count(".") < 2:
        return {}
    try:
        payload_segment = token.split(".", 2)[1]
        payload_segment += "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment.encode("ascii")))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return {name: payload[name] for name in SAFE_CLAIMS if name in payload}
