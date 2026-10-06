# Access control for the A4 fork of google-ads-mcp.
#
# The OAuth app is published ("In production"), so any Google account could
# sign in. This middleware lets through only the e-mail addresses listed in
# ADS_ALLOWED_EMAILS (comma separated, case-insensitive). If the variable is
# not set, only the owner's account is allowed. ADS_ALLOWED_EMAILS=* turns
# the check off (emergency switch). Applies to every MCP request
# (listing tools, calling tools, resources) when the server runs with OAuth.

"""E-mail allowlist for the hosted MCP server."""

import logging
import os
from typing import Any, Optional, Set

from fastmcp.exceptions import AuthorizationError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_EMAILS = "whabalo@gmail.com"


def allowed_emails() -> Set[str]:
    raw = os.environ.get("ADS_ALLOWED_EMAILS", DEFAULT_ALLOWED_EMAILS)
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _current_email() -> Optional[str]:
    token = get_access_token()
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}
    email = claims.get("email")
    return str(email).strip().lower() if email else ""


def check_access() -> None:
    """Raises AuthorizationError if the signed-in user is not allowed.

    No token (local stdio mode) means no OAuth, so nothing to check.
    """
    email = _current_email()
    if email is None:
        return
    allowed = allowed_emails()
    if "*" in allowed:  # emergency switch: ADS_ALLOWED_EMAILS=* disables the check
        return
    if email not in allowed:
        logger.warning("Access denied for %s", email or "<no e-mail claim>")
        raise AuthorizationError(
            f"Konto {email or '(brak e-maila)'} nie ma dostępu do tego serwera."
        )


class EmailAllowlistMiddleware(Middleware):
    async def on_request(self, context: Any, call_next: Any) -> Any:
        check_access()
        return await call_next(context)
