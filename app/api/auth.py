# app/api/auth.py
# monday.com OAuth 2.0 authentication flow
#
# Step 1: /install   → redirect user to monday.com login
# Step 2: /callback  → monday sends back code → exchange for token → save workspace
# Step 3: /me        → return current workspace info (protected)
# Step 4: /uninstall → mark workspace as uninstalled

from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from urllib.parse import urlencode
import httpx
import secrets

from app.core.config import settings
from app.core.database import get_supabase_admin
from app.core.jwt import create_access_token
from app.core.dependencies import get_current_workspace

# Create router
router = APIRouter()

# monday.com official OAuth endpoints — do not change these
MONDAY_AUTH_URL  = "https://auth.monday.com/oauth2/authorize"
MONDAY_TOKEN_URL = "https://auth.monday.com/oauth2/token"
MONDAY_API_URL   = "https://api.monday.com/v2"


# ─────────────────────────────────────────
# GET /api/v1/auth/install
# User clicks Install in monday.com marketplace
# We redirect them to monday.com login page
# ─────────────────────────────────────────
@router.get("/install")
async def install():

    # Generate random state token to prevent CSRF attacks
    state = secrets.token_urlsafe(16)

    # Build monday.com authorization URL
    params = {
        "client_id":    settings.monday_client_id,
        "redirect_uri": f"{settings.app_base_url}/api/v1/auth/callback",
        "state":        state,
        "scopes":       "me:read boards:read workspaces:read webhooks:write",
    }

    auth_url = f"{MONDAY_AUTH_URL}?{urlencode(params)}"

    # Redirect user to monday.com
    return RedirectResponse(url=auth_url)


# ─────────────────────────────────────────
# GET /api/v1/auth/callback
# monday.com redirects here after user approves
# Exchange code for access token → save workspace → return JWT
# ─────────────────────────────────────────
@router.get("/callback")
async def oauth_callback(
    code:  str = Query(...),        # Authorization code from monday
    state: str = Query(None),       # CSRF state (optional verify)
    error: str = Query(None),       # Error from monday (if user denied)
):

    # Handle user denying the install
    if error:
        raise HTTPException(
            status_code=400,
            detail=f"monday.com OAuth error: {error}"
        )

    # ── Step 1: Exchange authorization code for access token ──
    async with httpx.AsyncClient(timeout=30.0) as client:
        token_response = await client.post(
            MONDAY_TOKEN_URL,
            data={
                "client_id":     settings.monday_client_id,
                "client_secret": settings.monday_client_secret,
                "code":          code,
                "redirect_uri":  f"{settings.app_base_url}/api/v1/auth/callback",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

    if token_response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Token exchange failed: {token_response.text}"
        )

    token_data    = token_response.json()
    access_token  = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    if not access_token:
        raise HTTPException(
            status_code=400,
            detail="No access token received from monday.com"
        )

    # ── Step 2: Get workspace and user info from monday.com ──
    async with httpx.AsyncClient(timeout=30.0) as client:
        me_response = await client.post(
            MONDAY_API_URL,
            json={
                "query": """
                    query {
                        me {
                            id
                            name
                            email
                            account {
                                id
                                name
                            }
                        }
                    }
                """
            },
            headers={
                "Authorization": access_token,
                "Content-Type":  "application/json",
            }
        )

    if me_response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail="Failed to fetch user info from monday.com"
        )

    me_json = me_response.json()

    # Check for GraphQL errors
    if "errors" in me_json:
        raise HTTPException(
            status_code=400,
            detail=f"monday.com GraphQL error: {me_json['errors']}"
        )

    me_data           = me_json["data"]["me"]
    account           = me_data["account"]
    monday_user_id    = me_data["id"]
    monday_account_id = account["id"]
    workspace_name    = account["name"]
    user_email        = me_data.get("email", "")
    user_name         = me_data.get("name", "")

    # ── Step 3: Save workspace in Supabase (upsert) ──
    # If workspace already exists → update tokens
    # If new → insert
    db = get_supabase_admin()

    try:
        workspace_result = db.table("workspaces").upsert(
            {
                "monday_account_id":   monday_account_id,
                "monday_workspace_id": monday_account_id,
                "workspace_name":      workspace_name,
                "access_token":        access_token,
                "refresh_token":       refresh_token,
                "status":              "ACTIVE",
                "is_active":           True,
                "is_paused":           False,
                "plan_tier":           "FREE",
            },
            on_conflict="monday_workspace_id"
        ).execute()

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save workspace: {str(e)}"
        )

    workspace    = workspace_result.data[0]
    workspace_id = workspace["id"]

    # ── Step 4: Save user in Supabase (upsert) ──
    try:
        db.table("users").upsert(
            {
                "workspace_id":   workspace_id,
                "monday_user_id": monday_user_id,
                "email":          user_email,
                "name":           user_name,
                "role":           "ADMIN",   # first installer = admin
                "is_admin":       True,
            },
            on_conflict="workspace_id,monday_user_id"
        ).execute()

    except Exception as e:
        # Non-critical — continue even if user save fails
        pass

    # ── Step 5: Create default workspace settings (first time only) ──
    try:
        existing = db.table("workspace_settings")\
            .select("id")\
            .eq("workspace_id", workspace_id)\
            .execute()

        if not existing.data:
            db.table("workspace_settings").insert({
                "workspace_id":                workspace_id,
                "ai_sensitivity":              "BALANCED",
                "ai_enabled":                  True,
                "exact_match_fallback_enabled": True,
                "is_enabled":                  True,
                "onboarding_completed":        False,
            }).execute()

    except Exception:
        pass

    # ── Step 6: Log installation ──
    try:
        db.table("app_installations").insert({
            "workspace_id": workspace_id,
            "status":       "INSTALLED",
            "app_version":  "1.0.0",
        }).execute()

    except Exception:
        pass

    # ── Step 7: Create JWT token for this workspace ──
    jwt_token = create_access_token({
        "workspace_id": workspace_id,
        "plan_tier":    workspace["plan_tier"],
    })

    # Return JWT + workspace info to frontend
    return JSONResponse({
        "access_token":   jwt_token,
        "token_type":     "bearer",
        "workspace_id":   workspace_id,
        "workspace_name": workspace_name,
        "plan_tier":      workspace["plan_tier"],
    })


# ─────────────────────────────────────────
# GET /api/v1/auth/me
# Protected route — returns current workspace info
# Requires valid JWT in Authorization header
# ─────────────────────────────────────────
@router.get("/me")
async def get_me(workspace: dict = Depends(get_current_workspace)):
    # get_current_workspace dependency verifies JWT
    # and returns workspace dict from DB
    return {
        "id":             workspace["id"],
        "workspace_name": workspace["workspace_name"],
        "plan_tier":      workspace["plan_tier"],
        "status":         workspace["status"],
        "is_active":      workspace["is_active"],
        "is_paused":      workspace["is_paused"],
        "paused_reason":  workspace.get("paused_reason"),
        "installed_at":   workspace.get("installed_at"),
        "last_active_at": workspace.get("last_active_at"),
    }


# ─────────────────────────────────────────
# POST /api/v1/auth/uninstall
# Called when user uninstalls WizClone from monday.com
# Marks workspace as inactive in DB
# ─────────────────────────────────────────
@router.post("/uninstall")
async def uninstall(workspace: dict = Depends(get_current_workspace)):
    db = get_supabase_admin()

    # Mark workspace as uninstalled
    db.table("workspaces")\
        .update({
            "status":    "UNINSTALLED",
            "is_active": False,
        })\
        .eq("id", workspace["id"])\
        .execute()

    # Update installation log
    try:
        db.table("app_installations")\
            .update({"status": "UNINSTALLED"})\
            .eq("workspace_id", workspace["id"])\
            .execute()
    except Exception:
        pass

    return {
        "success": True,
        "message": "WizClone uninstalled successfully"
    }