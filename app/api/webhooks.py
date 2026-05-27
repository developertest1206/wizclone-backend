# app/api/webhooks.py
# ─────────────────────────────────────────────────────────────
# Webhook Receiver — monday.com sends all events here
#
# Flow:
#   1. monday.com POSTs event when new item is created
#   2. Verify signature (security)
#   3. Deduplicate (monday sometimes sends same event twice)
#   4. Find workspace by account_id
#   5. Check workspace is active and not paused
#   6. *** Check board is in monitored_boards and is_enabled ***
#   7. Check workspace-level automation is_enabled
#   8. Create automation_event log entry
#   9. Create queue_job for matching engine
#   10. Return fast 200 response (matching happens in background)
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from typing import Optional
import hmac
import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.database import get_supabase_admin


router = APIRouter()


# ─────────────────────────────────────────
# Verify monday.com webhook signature
# Ensures request is genuinely from monday.com
# ─────────────────────────────────────────
def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """
    monday.com signs every webhook request with HMAC-SHA256.
    We verify the signature using our webhook secret.
    If no secret is set → skip (development mode).
    """
    if not settings.monday_webhook_secret:
        return True  # Skip verification in development

    expected = hmac.new(
        settings.monday_webhook_secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


# ─────────────────────────────────────────
# Deduplication helpers
# monday.com sometimes sends same event twice
# ─────────────────────────────────────────
def is_duplicate_event(event_id: str) -> bool:
    """Check if this event_id was already received and processed"""
    db = get_supabase_admin()

    try:
        # Look for event_id in deduplication table
        # Look for event_id in deduplication table
        result = db.table("webhook_deduplication")\
            .select("event_id")\
            .eq("event_id", event_id)\
            .execute()

        # If any row found → duplicate
        return len(result.data) > 0

    except Exception:
        return False  # On error → treat as new event (safe side)


def mark_event_received(event_id: str, workspace_id: str, board_id: int, item_id: int):
    """Record event as received — expires after 7 days"""
    db         = get_supabase_admin()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    db.table("webhook_deduplication").insert({
        "event_id":     event_id,
        "workspace_id": workspace_id,
        "board_id":     board_id,
        "item_id":      item_id,
        "is_processed": False,
        "expires_at":   expires_at,
    }).execute()


# ─────────────────────────────────────────
# Board-level check
# Is this board being monitored AND enabled?
# ─────────────────────────────────────────
def get_board_status(workspace_id: str, board_id: int) -> dict:
    """
    Check if this board is in monitored_boards and is enabled.

    Returns:
        {
            "monitored":  True/False,   # Board is in our DB
            "enabled":    True/False,   # Board toggle is ON
            "sensitivity_override": "STRICT"/"BALANCED"/"LOOSE"/None
        }
    """
    db = get_supabase_admin()
    try:
        result = db.table("monitored_boards")\
            .select("is_enabled, sensitivity_override")\
            .eq("workspace_id", workspace_id)\
            .eq("board_id", board_id)\
            .is_("deleted_at", "null")\
            .execute()

        if not result.data:
            return {"monitored": False, "enabled": False, "sensitivity_override": None}

        board = result.data[0]
        return {
            "monitored":            True,
            "enabled":              board.get("is_enabled", True),
            "sensitivity_override": board.get("sensitivity_override"),
        }
    except Exception:
        return {"monitored": False, "enabled": False, "sensitivity_override": None}


# ─────────────────────────────────────────
# POST /api/v1/webhooks/monday
# Main webhook endpoint — monday.com POSTs here on every item creation
# ─────────────────────────────────────────
@router.post("/monday")
async def receive_monday_webhook(
    request: Request,
    x_monday_signature: Optional[str] = Header(None),
):
    """
    Receives item-created events from monday.com.
    Must respond within 3 seconds — actual processing happens in background queue.
    """

    # ── Step 1: Read raw body ──
    body = await request.body()

    # ── Step 2: Verify signature (security check) ──
    if x_monday_signature:
        if not verify_webhook_signature(body, x_monday_signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # ── Step 3: Parse JSON ──
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # ── Step 4: Handle monday.com challenge ──
    # monday sends a challenge when webhook is first registered
    # We must return same challenge value to confirm ownership
    if "challenge" in payload:
        return JSONResponse({"challenge": payload["challenge"]})

    # ── Step 5: Extract event data ──
    event = payload.get("event", {})
    
    if not event:
        return JSONResponse({"status": "ignored", "reason": "no event data"})

    # monday.com uses "pulse" as internal name for items
    event_id   = str(event.get("id", ""))
    item_id    = event.get("pulseId")
    item_name  = event.get("pulseName", "")
    board_id   = event.get("boardId")
    account_id = event.get("accountId")

    if not item_id or not board_id or not event_id:
        return JSONResponse({"status": "ignored", "reason": "missing required fields"})

    # ── Step 6: Find workspace ──
    db = get_supabase_admin()
    try:
        ws_result = db.table("workspaces")\
            .select("id, is_active, is_paused, plan_tier")\
            .eq("monday_account_id", account_id)\
            .eq("is_active", True)\
            .single()\
            .execute()
    except Exception:
        return JSONResponse({"status": "ignored", "reason": "workspace not found"})

    if not ws_result.data:
        return JSONResponse({"status": "ignored", "reason": "workspace not found"})

    workspace    = ws_result.data
    workspace_id = workspace["id"]

    # ── Step 7: Skip paused workspace ──
    if workspace.get("is_paused"):
        return JSONResponse({"status": "ignored", "reason": "workspace paused"})

    # ── Step 8: Check board-level monitoring ──
    # Only process events from boards the user has added via Settings
    board_status = get_board_status(workspace_id, board_id)

    if not board_status["monitored"]:
        # Board not added to WizClone — ignore silently
        return JSONResponse({"status": "ignored", "reason": "board not monitored"})

    if not board_status["enabled"]:
        # Board is monitored but toggled OFF — ignore silently
        # Log it so user can see in activity log
        return JSONResponse({"status": "ignored", "reason": "board disabled"})

    # ── Step 9: Check workspace-level automation toggle ──
    try:
        ws_settings = db.table("workspace_settings")\
            .select("is_enabled")\
            .eq("workspace_id", workspace_id)\
            .single()\
            .execute()

        if ws_settings.data and not ws_settings.data.get("is_enabled", True):
            # Whole workspace automation is OFF
            return JSONResponse({"status": "ignored", "reason": "workspace automation disabled"})
    except Exception:
        pass  # If settings fetch fails — continue processing

    # ── Step 10: Deduplicate ──
    if is_duplicate_event(event_id):
        return JSONResponse({"status": "ignored", "reason": "duplicate event"})

    # ── Step 11: Mark event as received ──
    try:
        mark_event_received(event_id, workspace_id, board_id, item_id)
    except Exception:
        # Race condition — another request processed this event first
        return JSONResponse({"status": "ignored", "reason": "duplicate event (race)"})

    # ── Step 12: Create automation event (activity log entry) ──
    try:
        auto_event = db.table("automation_events").insert({
            "workspace_id": workspace_id,
            "item_id":      item_id,
            "item_name":    item_name,
            "board_id":     board_id,
            "event_id":     event_id,
            "trigger_type": "WEBHOOK",
            "status":       "NO_MATCH",   # Will be updated by worker
        }).execute()

        automation_event_id = auto_event.data[0]["id"]

    except Exception as e:
        # Log failed but still return 200 — don't let monday.com retry
        return JSONResponse({"status": "error", "reason": str(e)})

    # ── Step 13: Create queue job for matching engine ──
    # Worker (worker.py) picks this up and runs matching + copy
    try:
        db.table("queue_jobs").insert({
            "workspace_id":        workspace_id,
            "automation_event_id": automation_event_id,
            "job_type":            "MATCHING",
            "status":              "PENDING",
            "payload": {
                "item_id":               item_id,
                "item_name":             item_name,
                "board_id":              board_id,
                "workspace_id":          workspace_id,
                "event_id":              event_id,
                "automation_event_id":   automation_event_id,
                "sensitivity_override":  board_status.get("sensitivity_override"),
            },
            "max_attempts": 3,
            "priority":     1,
        }).execute()
    except Exception:
        pass  # Queue failed — event is still logged

    # ── Step 14: Fast response — monday.com needs < 3 seconds ──
    return JSONResponse({
        "status":    "queued",
        "event_id":  event_id,
        "item_name": item_name,
    })


# ─────────────────────────────────────────
# GET /api/v1/webhooks/health
# Quick check that webhook endpoint is reachable
# ─────────────────────────────────────────
@router.get("/health")
async def webhook_health():
    return {
        "status":   "ok",
        "endpoint": "/api/v1/webhooks/monday",
        "method":   "POST"
    }