# app/api/boards.py
# ─────────────────────────────────────────────────────────────
# Screen 1 — Template Board Picker Dropdown
#
# Routes:
#   GET /api/v1/boards/{workspace_id}
#       → Fetches all boards from monday.com for this workspace
#       → Frontend shows them in the template board picker dropdown
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Depends
import httpx
from app.core.dependencies import get_current_workspace
from app.core.database import get_supabase_admin
from app.models.schemas import BoardsListResponse, BoardItem

router = APIRouter()

# monday.com GraphQL API endpoint
MONDAY_API_URL = "https://api.monday.com/v2"


# ─────────────────────────────────────────
# GET /api/v1/boards/{workspace_id}
# Called when Settings page opens board picker dropdown
# Fetches all boards from this workspace via monday.com API
# ─────────────────────────────────────────
@router.get(
    "/boards/{workspace_id}",
    response_model=BoardsListResponse
)
async def get_boards(
    workspace_id: str,
    workspace: dict = Depends(get_current_workspace)    # JWT protected
):
    # Security check
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Get the workspace access token to call monday.com API
    db = get_supabase_admin()
    try:
        ws_result = db.table("workspaces")\
            .select("access_token")\
            .eq("id", workspace_id)\
            .single()\
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Workspace not found")

    access_token = ws_result.data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="No monday.com access token found")

    # GraphQL query — fetch all boards in the workspace
    query = """
        query {
            boards(limit: 100, order_by: created_at) {
                id
                name
            }
        }
    """

    # Call monday.com GraphQL API
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            MONDAY_API_URL,
            json={"query": query},
            headers={
                "Authorization": access_token,
                "Content-Type":  "application/json",
            }
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch boards from monday.com"
        )

    data = response.json()

    # Check for GraphQL errors
    if "errors" in data:
        raise HTTPException(
            status_code=502,
            detail=f"monday.com error: {data['errors']}"
        )

    # Parse boards list
    raw_boards = data.get("data", {}).get("boards", [])

    boards = [
        BoardItem(id=str(b["id"]), name=b["name"])
        for b in raw_boards
        if b.get("id") and b.get("name")
    ]

    return BoardsListResponse(boards=boards)