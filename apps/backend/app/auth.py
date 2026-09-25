"""Supabase Auth session verification.

The frontend authenticates directly against Supabase Auth (email/password)
using the publishable/anon key and attaches the resulting access token to
every API call as ``Authorization: Bearer <token>``. This module verifies
that token server-side on every request — the backend never sees a password
and never holds the Supabase service-role key.

Verification prefers Supabase's current asymmetric signing (RS256/ES256) via
its JWKS endpoint (cached, matching Supabase's own token TTL assumptions). A
legacy HS256 shared-secret path is supported for older projects that still
sign access tokens symmetrically (``SUPABASE_JWT_SECRET``).
"""

import logging
import time
from typing import Any

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import settings

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

# Cache the JWKS client (and thus its keys) for a while so we don't fetch on
# every request; PyJWKClient itself also caches per-kid lookups.
_jwks_client: PyJWKClient | None = None
_jwks_client_built_at: float = 0.0
_JWKS_CLIENT_TTL_SECONDS = 3600


def _get_jwks_client() -> PyJWKClient | None:
    global _jwks_client, _jwks_client_built_at
    jwks_url = settings.effective_jwks_url
    if not jwks_url:
        return None
    now = time.monotonic()
    if _jwks_client is None or now - _jwks_client_built_at > _JWKS_CLIENT_TTL_SECONDS:
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=_JWKS_CLIENT_TTL_SECONDS)
        _jwks_client_built_at = now
    return _jwks_client


def _decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a Supabase Auth access token. Raises jwt exceptions on failure."""
    jwks_client = _get_jwks_client()
    if jwks_client is not None:
        try:
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience="authenticated",
                options={"require": ["exp", "sub"]},
            )
        except jwt.PyJWKClientError as exc:
            # No matching key in JWKS — fall through to the HS256 secret path
            # (covers projects still on legacy symmetric signing) if configured.
            if not settings.supabase_jwt_secret:
                raise jwt.InvalidTokenError(str(exc)) from exc

    if settings.supabase_jwt_secret:
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
            options={"require": ["exp", "sub"]},
        )

    raise jwt.InvalidTokenError(
        "No Supabase JWKS URL or JWT secret configured; cannot verify tokens."
    )


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """FastAPI dependency: verify the bearer token, return the caller's user id.

    Every endpoint that touches user-owned data depends on this so ownership
    can be enforced both in the facade (``app/database.py``) and by Postgres
    RLS policies keyed on the same id.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = _decode_token(credentials.credentials)
    except httpx.HTTPError as exc:
        logger.error(f"JWKS fetch failed during token verification: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth verification temporarily unavailable. Please try again.",
        ) from exc
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        logger.warning(f"Rejected invalid bearer token: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    subject = claims.get("sub")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return str(subject)


async def get_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """FastAPI dependency: the raw (already-verified-elsewhere) bearer token string.

    Used only where the caller's token itself must be forwarded onward — e.g.
    PDF rendering, which navigates a headless-browser request back to the
    frontend's print route (see app/pdf.py::render_resume_pdf) and needs to
    present the *same* caller's credentials, not just their id. Always pair
    this with ``get_current_user_id`` on the same endpoint so the token is
    still verified, not merely forwarded.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials
