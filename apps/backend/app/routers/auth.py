"""Auth introspection endpoint.

Login/signup/logout happen client-side against Supabase Auth directly (the
frontend holds the publishable key and calls Supabase's own REST API — the
backend is never in that path and never sees a password). This router only
exposes a way for the frontend to confirm a bearer token is valid and see
which account it belongs to.
"""

from fastapi import APIRouter, Depends

from app.auth import get_current_user_id

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me")
async def get_current_session(user_id: str = Depends(get_current_user_id)) -> dict[str, str]:
    """Return the authenticated caller's user id (verifies the bearer token)."""
    return {"user_id": user_id}
