"""JWT verification helper for Cognito tokens."""
import json
import os
from functools import lru_cache
from typing import Any

import urllib.request
import urllib.error


USER_POOL_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
USER_POOL_CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")

JWKS_URL = (
    f"https://cognito-idp.{USER_POOL_REGION}.amazonaws.com"
    f"/{USER_POOL_ID}/.well-known/jwks.json"
)


@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    with urllib.request.urlopen(JWKS_URL, timeout=5) as resp:
        return json.loads(resp.read())


def get_user_from_event(event: dict) -> dict[str, Any] | None:
    """
    API Gateway HTTP API passes Cognito claims in requestContext.authorizer.jwt.claims
    when the JWT authorizer is configured.  Returns the claims dict or None.
    """
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]
    except (KeyError, TypeError):
        return None


def require_auth(event: dict) -> dict[str, Any]:
    """Return claims or raise ValueError."""
    claims = get_user_from_event(event)
    if not claims:
        raise ValueError("Unauthorized")
    return claims
