# app/core/dependencies.py
# ─────────────────────────────────────────────────────────────
# FastAPI dependency injection
# Use Depends(get_current_workspace) on any protected route
# It verifies JWT and returns the workspace dict from DB
# ─────────────────────────────────────────────────────────────

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.jwt import verify_access_token
from app.core.database import get_supabase_admin

# This reads the Bearer token from Authorization header automatically
bearer_scheme = HTTPBearer()


async def get_current_workspace(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)
) -> dict:
    """
    Protected route dependency.
    Usage in any route:
        async def my_route(workspace=Depends(get_current_workspace)):

    Flow:
    1. Read Bearer token from Authorization header
    2. Verify JWT signature + expiry
    3. Extract workspace_id from token payload
    4. Fetch workspace from Supabase DB
    5. Return workspace dict to the route
    """

    token = credentials.credentials

    # Step 1: Verify JWT token
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please re-authenticate."
        )

    # Step 2: Extract workspace_id from token
    workspace_id = payload.get("workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing workspace_id"
        )

    # Step 3: Fetch workspace from DB and verify it's still active
    db = get_supabase_admin()
    try:
        result = db.table("workspaces")\
            .select("*")\
            .eq("id", workspace_id)\
            .eq("is_active", True)\
            .single()\
            .execute()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Workspace not found or has been deactivated"
        )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Workspace not found or has been deactivated"
        )

    # Return full workspace dict to the route handler
    return result.data