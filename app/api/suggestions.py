# app/api/suggestions.py
# ─────────────────────────────────────────────────────────────
# Screen 1 — AI Suggestion Banner
#
# When WizClone detects repeated patterns, it creates a suggestion.
# Frontend shows a banner: "We noticed you keep adding similar subtasks.
# Want to turn this into a template?"
#
# Routes:
#   GET  /api/v1/suggestions/{workspace_id}            → get active suggestions
#   POST /api/v1/suggestions/{suggestion_id}/accept    → user clicks Yes
#   POST /api/v1/suggestions/{suggestion_id}/dismiss   → user clicks Dismiss
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timezone, timedelta
from app.core.database import get_supabase_admin
from app.core.dependencies import get_current_workspace
from app.models.schemas import SuggestionResponse, SuggestionAcceptRequest, SuccessResponse

router = APIRouter()


# ─────────────────────────────────────────
# GET /api/v1/suggestions/{workspace_id}
# Returns active (PENDING) suggestions for this workspace
# Frontend shows these as banners on Settings page
# ─────────────────────────────────────────
@router.get(
    "/suggestions/{workspace_id}",
    response_model=list[SuggestionResponse]
)
async def get_suggestions(
    workspace_id: str,
    workspace: dict = Depends(get_current_workspace)
):
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()
    now = datetime.now(timezone.utc).isoformat()

    # Fetch PENDING suggestions that are not currently dismissed
    # dismissed_until = NULL means never dismissed
    # dismissed_until < now means dismiss period has expired — show again
    result = db.table("ai_suggestions")\
        .select("*")\
        .eq("workspace_id", workspace_id)\
        .eq("status", "PENDING")\
        .is_("deleted_at", "null")\
        .execute()

    suggestions_data = result.data or []

    # Filter out suggestions that are currently in dismiss window
    active = []
    for s in suggestions_data:
        dismissed_until = s.get("dismissed_until")
        if dismissed_until and dismissed_until > now:
            # Still in dismiss window — skip
            continue
        active.append(s)

    return [
        SuggestionResponse(
            id                      = s["id"],
            suggested_template_name = s.get("suggested_template_name", ""),
            detected_item_names     = s.get("detected_item_names", []),
            occurrence_count        = s.get("occurrence_count", 0),
            suggested_subitems      = s.get("suggested_subitems", []),
            status                  = s.get("status", "PENDING"),
            confidence_score        = s.get("confidence_score"),
            created_at              = s["created_at"],
        )
        for s in active
    ]


# ─────────────────────────────────────────
# POST /api/v1/suggestions/{suggestion_id}/accept
# User clicks "Yes" on suggestion banner
# Creates a real template from the suggestion
# ─────────────────────────────────────────
@router.post(
    "/suggestions/{suggestion_id}/accept",
    response_model=SuccessResponse
)
async def accept_suggestion(
    suggestion_id: str,
    body: SuggestionAcceptRequest,
    workspace: dict = Depends(get_current_workspace)
):
    if workspace["id"] != body.workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()

    # Fetch the suggestion
    try:
        suggestion_result = db.table("ai_suggestions")\
            .select("*")\
            .eq("id", suggestion_id)\
            .eq("workspace_id", body.workspace_id)\
            .single()\
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    if not suggestion_result.data:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    suggestion = suggestion_result.data

    # Create a new template from this suggestion
    try:
        template_result = db.table("templates").insert({
            "workspace_id":     body.workspace_id,
            "name":             suggestion.get("suggested_template_name", "New Template"),
            "source":           "SUGGESTION",       # Marks as auto-suggested
            "created_by_ai":    True,
            "is_active":        True,
            "is_deleted":       False,
            "usage_count":      0,
        }).execute()

        template_id = template_result.data[0]["id"]

        # Insert suggested subitems
        suggested_subitems = suggestion.get("suggested_subitems", [])
        if suggested_subitems and isinstance(suggested_subitems, list):
            subitems_to_insert = []
            for idx, s in enumerate(suggested_subitems):
                if isinstance(s, dict):
                    subitems_to_insert.append({
                        "template_id":  template_id,
                        "name":         s.get("name", f"Step {idx + 1}"),
                        "sort_order":   s.get("sort_order", idx + 1),
                    })
            if subitems_to_insert:
                db.table("template_subitems").insert(subitems_to_insert).execute()

        # Update suggestion — mark as ACCEPTED, link to created template
        db.table("ai_suggestions")\
            .update({
                "status":               "ACCEPTED",
                "accepted_template_id": template_id,
            })\
            .eq("id", suggestion_id)\
            .execute()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create template: {str(e)}")

    return SuccessResponse(message="Template created from suggestion successfully")


# ─────────────────────────────────────────
# POST /api/v1/suggestions/{suggestion_id}/dismiss
# User clicks "Dismiss" on suggestion banner
# Hides suggestion for 30 days
# ─────────────────────────────────────────
@router.post(
    "/suggestions/{suggestion_id}/dismiss",
    response_model=SuccessResponse
)
async def dismiss_suggestion(
    suggestion_id: str,
    workspace: dict = Depends(get_current_workspace)
):
    db = get_supabase_admin()

    # Set dismissed_until = 30 days from now
    dismissed_until = (
        datetime.now(timezone.utc) + timedelta(days=30)
    ).isoformat()

    try:
        db.table("ai_suggestions")\
            .update({
                "dismissed_until": dismissed_until,
                # Keep status as PENDING — will show again after 30 days
            })\
            .eq("id", suggestion_id)\
            .eq("workspace_id", workspace["id"])\
            .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to dismiss: {str(e)}")

    return SuccessResponse(message="Suggestion dismissed for 30 days")