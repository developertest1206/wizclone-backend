# app/api/activity.py
# ─────────────────────────────────────────────────────────────
# Screen 3 — Activity Log
#
# Routes:
#   GET /api/v1/activity/{workspace_id}       → last 50 events with tab filters
#   GET /api/v1/activity/{event_id}/detail    → full detail of one event
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional
from app.core.database import get_supabase_admin
from app.core.dependencies import get_current_workspace
from app.models.schemas import (
    ActivityLogResponse, ActivityEventResponse,
    ActivityEventDetail, EventStatus
)

router = APIRouter()


# ─────────────────────────────────────────
# GET /api/v1/activity/{workspace_id}
# Screen 3 — Main activity log table
# Supports tab filters: ALL / SUCCESS / NO_MATCH / FAILED
# Returns last 50 events
# ─────────────────────────────────────────
@router.get(
    "/activity/{workspace_id}",
    response_model=ActivityLogResponse
)
async def get_activity_log(
    workspace_id: str,
    # Tab filter — matches event status values
    status: Optional[str] = Query(None, description="Filter: SUCCESS, NO_MATCH, FAILED, PARTIAL_SUCCESS"),
    limit:  int           = Query(50, ge=1, le=100),
    workspace: dict = Depends(get_current_workspace)
):
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()

    # Build query — newest events first
    query = db.table("automation_events")\
        .select("*")\
        .eq("workspace_id", workspace_id)\
        .order("created_at", desc=True)\
        .limit(limit)

    # Apply status filter if tab is selected
    # ALL tab → no filter
    # SUCCESS tab → status=SUCCESS
    # NO MATCH tab → status=NO_MATCH
    # FAILED tab → status=FAILED or PARTIAL_SUCCESS
    if status and status.upper() != "ALL":
        if status.upper() == "FAILED":
            # FAILED tab shows both FAILED and PARTIAL_SUCCESS
            query = db.table("automation_events")\
                .select("*")\
                .eq("workspace_id", workspace_id)\
                .in_("status", ["FAILED", "PARTIAL_SUCCESS"])\
                .order("created_at", desc=True)\
                .limit(limit)
        else:
            query = db.table("automation_events")\
                .select("*")\
                .eq("workspace_id", workspace_id)\
                .eq("status", status.upper())\
                .order("created_at", desc=True)\
                .limit(limit)

    result = query.execute()
    events_data = result.data or []

    # Map DB rows to response model
    events = [
        ActivityEventResponse(
            id                      = e["id"],
            item_name               = e.get("item_name", "Unknown"),
            board_name              = e.get("board_name"),
            matched_template_name   = e.get("matched_template_name"),
            match_method            = e.get("match_method"),
            confidence_score        = e.get("confidence_score"),
            subitems_copied         = e.get("subitems_copied", 0),
            subitems_failed         = e.get("subitems_failed", 0),
            status                  = e.get("status", "NO_MATCH"),
            ai_fallback_used        = e.get("ai_fallback_used", False),
            processing_ms           = e.get("processing_ms"),
            created_at              = e["created_at"],
        )
        for e in events_data
    ]

    return ActivityLogResponse(
        events  = events,
        total   = len(events)
    )


# ─────────────────────────────────────────
# GET /api/v1/activity/{event_id}/detail
# Screen 3 — Click on a row to see full event detail
# Shows: failed subitems, error message, retry count etc.
# ─────────────────────────────────────────
@router.get(
    "/activity/{event_id}/detail",
    response_model=ActivityEventDetail
)
async def get_activity_event_detail(
    event_id: str,
    workspace: dict = Depends(get_current_workspace)
):
    db = get_supabase_admin()

    # Fetch full event detail
    try:
        result = db.table("automation_events")\
            .select("*")\
            .eq("id", event_id)\
            .single()\
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Event not found")

    if not result.data:
        raise HTTPException(status_code=404, detail="Event not found")

    e = result.data

    # Security — verify event belongs to current workspace
    if e.get("workspace_id") != workspace["id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return ActivityEventDetail(
        id                      = e["id"],
        item_id                 = e.get("item_id", 0),
        item_name               = e.get("item_name", "Unknown"),
        board_id                = e.get("board_id"),
        board_name              = e.get("board_name"),
        matched_template_id     = e.get("matched_template_id"),
        matched_template_name   = e.get("matched_template_name"),
        match_method            = e.get("match_method"),
        confidence_score        = e.get("confidence_score"),
        ai_sensitivity_used     = e.get("ai_sensitivity_used"),
        trigger_type            = e.get("trigger_type"),
        subitems_copied         = e.get("subitems_copied", 0),
        subitems_failed         = e.get("subitems_failed", 0),
        failed_subitem_names    = e.get("failed_subitem_names", []),
        status                  = e.get("status", "NO_MATCH"),
        error_details           = e.get("error_details"),
        ai_fallback_used        = e.get("ai_fallback_used", False),
        retry_count             = e.get("retry_count", 0),
        processing_ms           = e.get("processing_ms"),
        created_at              = e["created_at"],
    )