# app/api/boards.py
# ─────────────────────────────────────────────────────────────
# Boards API — Monitored Boards Management
#
# Flow when user adds a board:
#   1. Frontend sends board_id + board_name to backend
#   2. Backend saves board in monitored_boards table (is_enabled=True)
#   3. Backend calls monday.com API to create webhook automation:
#      "When item is created → POST to our webhook URL"
#   4. From now on, every new item on that board fires our webhook
#
# Toggle (enable/disable) is SERVER-SIDE ONLY:
#   - Does NOT touch monday.com at all
#   - Webhook still fires from monday.com
#   - Backend checks is_enabled — if False → ignore silently
#
# Routes:
#   GET    /api/v1/boards/{workspace_id}              → all boards (for dropdown)
#   GET    /api/v1/boards/{workspace_id}/monitored    → added boards list
#   POST   /api/v1/boards/{workspace_id}/add          → add board + register webhook
#   PATCH  /api/v1/boards/{workspace_id}/{board_id}   → enable / disable toggle
#   DELETE /api/v1/boards/{workspace_id}/{board_id}   → remove board + delete webhook
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import httpx
from datetime import datetime, timezone

from app.core.dependencies import get_current_workspace
from app.core.database import get_supabase_admin
from app.core.config import settings
from app.models.schemas import SuccessResponse

router = APIRouter()

# monday.com GraphQL API endpoint
MONDAY_API_URL = "https://api.monday.com/v2"


# ─────────────────────────────────────────
# Request / Response Models
# ─────────────────────────────────────────

class AddBoardRequest(BaseModel):
    """Frontend sends this when user picks a board from dropdown"""
    board_id:             int
    board_name:           str
    sensitivity_override: Optional[str] = None  # None = use workspace default


class BoardToggleRequest(BaseModel):
    """Enable or disable a specific board"""
    is_enabled: bool


class MonitoredBoardResponse(BaseModel):
    """Single board row in Settings page board list"""
    id:                   str
    board_id:             int
    board_name:           str
    webhook_id:           Optional[str] = None
    webhook_status:       str           = "ACTIVE"
    is_enabled:           bool          = True
    ai_enabled:           bool          = True
    sensitivity_override: Optional[str] = None
    created_at:           str


# ─────────────────────────────────────────
# monday.com GraphQL Helpers
# ─────────────────────────────────────────

async def create_monday_webhook(board_id: int, webhook_url: str, access_token: str) -> dict:
    """
    Register "When item is created → send webhook" automation on monday.com.

    Uses monday.com GraphQL create_webhook mutation.
    event: create_pulse = "when new item is created" in monday.com terminology
    """
    mutation = """
        mutation CreateWebhook($board_id: ID!, $url: String!, $event: WebhookEventType!) {
            create_webhook(board_id: $board_id, url: $url, event: $event) {
                id
                board_id
            }
        }
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                MONDAY_API_URL,
                json={
                    "query":     mutation,
                    "variables": {
                        "board_id": str(board_id),
                        "url":      webhook_url,
                        "event":    "create_pulse",   # create_pulse = item created
                    }
                },
                headers={
                    "Authorization": access_token,
                    "Content-Type":  "application/json",
                    "API-Version":   "2024-01",
                }
            )

        if response.status_code != 200:
            return {"success": False, "webhook_id": None, "error": f"HTTP {response.status_code}"}

        data = response.json()

        if "errors" in data:
            return {"success": False, "webhook_id": None, "error": str(data["errors"])}

        webhook_id = data.get("data", {}).get("create_webhook", {}).get("id")

        if not webhook_id:
            return {"success": False, "webhook_id": None, "error": "No webhook ID returned"}

        return {"success": True, "webhook_id": str(webhook_id), "error": None}

    except Exception as e:
        return {"success": False, "webhook_id": None, "error": str(e)}


async def delete_monday_webhook(webhook_id: str, access_token: str) -> bool:
    """
    Delete webhook from monday.com when board is removed.
    Returns True if deleted, False if failed (non-critical — we soft-delete from DB anyway).
    """
    mutation = """
        mutation DeleteWebhook($webhook_id: ID!) {
            delete_webhook(id: $webhook_id) {
                id
            }
        }
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                MONDAY_API_URL,
                json={"query": mutation, "variables": {"webhook_id": str(webhook_id)}},
                headers={
                    "Authorization": access_token,
                    "Content-Type":  "application/json",
                    "API-Version":   "2024-01",
                }
            )
        data = response.json()
        return "errors" not in data and response.status_code == 200
    except Exception:
        return False


async def get_workspace_token(db, workspace_id: str) -> str:
    """Fetch access_token for a workspace from DB"""
    result = db.table("workspaces")\
        .select("access_token")\
        .eq("id", workspace_id)\
        .single()\
        .execute()
    return result.data.get("access_token") if result.data else None


# ─────────────────────────────────────────
# GET /api/v1/boards/{workspace_id}
# Returns all boards from monday.com — for dropdown picker
# ─────────────────────────────────────────
@router.get("/boards/{workspace_id}")
async def get_all_boards(
    workspace_id: str,
    workspace: dict = Depends(get_current_workspace)
):
    """Fetch all workspace boards from monday.com for the board picker dropdown"""
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db           = get_supabase_admin()
    access_token = await get_workspace_token(db, workspace_id)

    if not access_token:
        raise HTTPException(status_code=401, detail="No monday.com access token")

    # GraphQL query — fetch all boards in the workspace
    query = """
        query {
            boards(limit: 100, order_by: created_at) {
                id
                name
                board_kind
            }
        }
    """

    # Call monday.com GraphQL API
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            MONDAY_API_URL,
            json={"query": query},
            headers={"Authorization": access_token, "Content-Type": "application/json", "API-Version": "2024-01"}
        )

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch boards from monday.com")

    data = response.json()

    # Check for GraphQL errors
    if "errors" in data:
        raise HTTPException(status_code=502, detail=str(data["errors"]))

    boards = [
        {"id": str(b["id"]), "name": b["name"]}
        for b in data.get("data", {}).get("boards", [])
        if b.get("id") and b.get("name")
    ]

    return {"boards": boards, "total": len(boards)}


# ─────────────────────────────────────────
# GET /api/v1/boards/{workspace_id}/monitored
# Returns boards currently being monitored by WizClone
# ─────────────────────────────────────────
@router.get("/boards/{workspace_id}/monitored")
async def get_monitored_boards(
    workspace_id: str,
    workspace: dict = Depends(get_current_workspace)
):
    """Returns all boards WizClone is watching — shown in Settings page board list"""
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db     = get_supabase_admin()
    result = db.table("monitored_boards")\
        .select("*")\
        .eq("workspace_id", workspace_id)\
        .is_("deleted_at", "null")\
        .order("created_at")\
        .execute()

    boards = result.data or []

    return {
        "boards": [
            {
                "id":                   b["id"],
                "board_id":             b["board_id"],
                "board_name":           b.get("board_name", ""),
                "webhook_id":           b.get("webhook_id"),
                "webhook_status":       b.get("webhook_status", "ACTIVE"),
                "is_enabled":           b.get("is_enabled", True),
                "ai_enabled":           b.get("ai_enabled", True),
                "sensitivity_override": b.get("sensitivity_override"),
                "created_at":           b["created_at"],
            }
            for b in boards
        ],
        "total": len(boards)
    }


# ─────────────────────────────────────────
# POST /api/v1/boards/{workspace_id}/add
# Add a board + register monday.com webhook automation
# ─────────────────────────────────────────
@router.post("/boards/{workspace_id}/add")
async def add_board(
    workspace_id: str,
    body:         AddBoardRequest,
    workspace:    dict = Depends(get_current_workspace)
):
    """
    Add a board to WizClone monitoring.

    Steps:
    1. Check board not already added
    2. Register webhook on monday.com: "when item created → call our URL"
    3. Save board + webhook_id in monitored_boards table
    """
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()

    # Step 1 — Check if already monitoring this board
    existing = db.table("monitored_boards")\
        .select("id")\
        .eq("workspace_id", workspace_id)\
        .eq("board_id", body.board_id)\
        .is_("deleted_at", "null")\
        .execute()

    if existing.data:
        raise HTTPException(status_code=409, detail=f"Board {body.board_id} is already being monitored")

    # Step 2 — Get access token
    access_token = await get_workspace_token(db, workspace_id)
    if not access_token:
        raise HTTPException(status_code=401, detail="No monday.com access token found")

    # Step 3 — Register webhook on monday.com
    # This URL is what monday.com will POST to when a new item is created
    webhook_url    = f"{settings.app_base_url}/api/v1/webhooks/monday"
    webhook_result = await create_monday_webhook(body.board_id, webhook_url, access_token)

    webhook_id     = None
    webhook_status = "ACTIVE"

    if webhook_result["success"]:
        webhook_id = webhook_result["webhook_id"]
        print(f"[Boards] Webhook registered on monday.com — board: {body.board_id}, webhook_id: {webhook_id}")
    else:
        # Save board anyway with FAILED status — admin can retry
        webhook_status = "FAILED"
        print(f"[Boards] Webhook registration FAILED for board {body.board_id}: {webhook_result['error']}")

    # Step 4 — Save board in DB
    try:
        result = db.table("monitored_boards").insert({
            "workspace_id":         workspace_id,
            "board_id":             body.board_id,
            "board_name":           body.board_name,
            "webhook_id":           webhook_id,
            "webhook_status":       webhook_status,
            "is_enabled":           True,
            "ai_enabled":           True,
            "sensitivity_override": body.sensitivity_override,
            "last_sync_at":         datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save board: {str(e)}")

    board = result.data[0]

    return {
        "id":             board["id"],
        "board_id":       board["board_id"],
        "board_name":     board.get("board_name", ""),
        "webhook_id":     board.get("webhook_id"),
        "webhook_status": board.get("webhook_status", "ACTIVE"),
        "is_enabled":     True,
        "created_at":     board["created_at"],
        "message":        "Board added and webhook registered successfully" if webhook_id else "Board added but webhook registration failed — please retry",
    }


# ─────────────────────────────────────────
# PATCH /api/v1/boards/{workspace_id}/{board_id}
# Enable or disable a board — SERVER-SIDE FILTER ONLY
# ─────────────────────────────────────────
@router.patch("/boards/{workspace_id}/{board_id}", response_model=SuccessResponse)
async def toggle_board(
    workspace_id: str,
    board_id:     int,
    body:         BoardToggleRequest,
    workspace:    dict = Depends(get_current_workspace)
):
    """
    Enable or disable WizClone automation for a specific board.

    IMPORTANT — Server-side filter only:
    - monday.com webhook is NOT touched (stays registered)
    - Webhook events still arrive at our server
    - Backend checks is_enabled before processing
    - If False → event logged as 'board_disabled' and ignored
    - If True  → normal AI matching + copy flow runs
    """
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()

    check = db.table("monitored_boards")\
        .select("id")\
        .eq("workspace_id", workspace_id)\
        .eq("board_id", board_id)\
        .is_("deleted_at", "null")\
        .execute()

    if not check.data:
        raise HTTPException(status_code=404, detail="Board not found")

    db.table("monitored_boards")\
        .update({"is_enabled": body.is_enabled})\
        .eq("workspace_id", workspace_id)\
        .eq("board_id", board_id)\
        .execute()

    status = "enabled" if body.is_enabled else "disabled"
    return SuccessResponse(message=f"Board automation {status}")


# ─────────────────────────────────────────
# DELETE /api/v1/boards/{workspace_id}/{board_id}
# Remove board + delete monday.com webhook
# ─────────────────────────────────────────
@router.delete("/boards/{workspace_id}/{board_id}", response_model=SuccessResponse)
async def remove_board(
    workspace_id: str,
    board_id:     int,
    workspace:    dict = Depends(get_current_workspace)
):
    """
    Remove board from WizClone monitoring.
    Deletes the webhook from monday.com so events stop coming.
    """
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()

    board_result = db.table("monitored_boards")\
        .select("id, webhook_id")\
        .eq("workspace_id", workspace_id)\
        .eq("board_id", board_id)\
        .is_("deleted_at", "null")\
        .execute()

    if not board_result.data:
        raise HTTPException(status_code=404, detail="Board not found")

    board      = board_result.data[0]
    webhook_id = board.get("webhook_id")

    # Delete webhook from monday.com (stops events from coming)
    if webhook_id:
        access_token = await get_workspace_token(db, workspace_id)
        if access_token:
            deleted = await delete_monday_webhook(webhook_id, access_token)
            print(f"[Boards] Webhook {webhook_id} {'deleted from' if deleted else 'failed to delete from'} monday.com")

    # Soft delete from DB
    db.table("monitored_boards")\
        .update({
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "is_active":  False,
        })\
        .eq("id", board["id"])\
        .execute()

    return SuccessResponse(message=f"Board removed from monitoring")