# app/api/settings_routes.py
# ─────────────────────────────────────────────────────────────
# Screen 1 — Settings Page API
#
# Routes:
#   GET  /api/v1/settings/{workspace_id}  → fetch current settings
#   POST /api/v1/settings/{workspace_id}  → save updated settings
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Depends
from app.core.database import get_supabase_admin
from app.core.dependencies import get_current_workspace
from app.models.schemas import WorkspaceSettingsResponse, WorkspaceSettingsUpdate, SuccessResponse

router = APIRouter()


# ─────────────────────────────────────────
# GET /api/v1/settings/{workspace_id}
# Frontend calls this when Settings page opens
# Returns current settings for this workspace
# ─────────────────────────────────────────
@router.get(
    "/settings/{workspace_id}",
    response_model=WorkspaceSettingsResponse
)
async def get_settings(
    workspace_id: str,
    workspace: dict = Depends(get_current_workspace)   # JWT protected
):
    # Security check — user can only read their own workspace settings
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()

    try:
        result = db.table("workspace_settings")\
            .select("*")\
            .eq("workspace_id", workspace_id)\
            .single()\
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Settings not found")

    if not result.data:
        raise HTTPException(status_code=404, detail="Settings not found")

    return result.data


# ─────────────────────────────────────────
# POST /api/v1/settings/{workspace_id}
# Frontend calls this when user saves settings
# Updates template board, sensitivity, toggles etc.
# ─────────────────────────────────────────
@router.post(
    "/settings/{workspace_id}",
    response_model=SuccessResponse
)
async def update_settings(
    workspace_id: str,
    body: WorkspaceSettingsUpdate,
    workspace: dict = Depends(get_current_workspace)   # JWT protected
):
    # Security check
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()

    # Build update dict — only include fields that were actually sent
    update_data = {}

    if body.template_board_id is not None:
        update_data["template_board_id"]   = body.template_board_id
        # Reset deleted flag when board is changed
        update_data["template_board_deleted"] = False

    if body.template_board_name is not None:
        update_data["template_board_name"] = body.template_board_name

    if body.ai_sensitivity is not None:
        update_data["ai_sensitivity"]      = body.ai_sensitivity.value

    if body.ai_enabled is not None:
        update_data["ai_enabled"]          = body.ai_enabled

    if body.exact_match_fallback_enabled is not None:
        update_data["exact_match_fallback_enabled"] = body.exact_match_fallback_enabled

    if body.is_enabled is not None:
        update_data["is_enabled"]          = body.is_enabled

    if not update_data:
        return SuccessResponse(message="No changes to save")

    try:
        db.table("workspace_settings")\
            .update(update_data)\
            .eq("workspace_id", workspace_id)\
            .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update settings: {str(e)}")

    return SuccessResponse(message="Settings saved successfully")