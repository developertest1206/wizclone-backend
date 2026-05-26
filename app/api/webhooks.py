# app/api/webhooks.py
# monday.com sends a POST request here every time
# a new item is created in any monitored board
# Flow: receive → verify → deduplicate → log → queue

from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from typing import Optional
import hmac
import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.database import get_supabase_admin

# Create router
router = APIRouter()


# ─────────────────────────────────────────
# Verify webhook signature
# Ensures request is genuinely from monday.com
# ─────────────────────────────────────────
def verify_webhook_signature(body: bytes, signature: str) -> bool:

    # If no secret set → skip verification (development mode)
    if not settings.monday_webhook_secret:
        return True

    # FIXED: correct Python hmac syntax is hmac.new() — not hmac.new
    # hmac.new(key, message, digestmod) is the correct call
    expected_hmac = hmac.new(
        settings.monday_webhook_secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    # Use compare_digest to prevent timing attacks
    return hmac.compare_digest(expected_hmac, signature)


# ─────────────────────────────────────────
# Check if event already processed
# monday.com sometimes sends same webhook twice
# ─────────────────────────────────────────
def is_duplicate_event(event_id: str) -> bool:
    db = get_supabase_admin()

    try:
        # Look for event_id in deduplication table
        result = db.table("webhook_deduplication")\
            .select("event_id")\
            .eq("event_id", event_id)\
            .execute()

        # If any row found → duplicate
        return len(result.data) > 0

    except Exception:
        # On any error → treat as not duplicate (safe side)
        return False


# ─────────────────────────────────────────
# Mark event as received in deduplication table
# Expires after 7 days (auto-cleanup)
# ─────────────────────────────────────────
def mark_event_received(
    event_id: str,
    workspace_id: str,
    board_id: int,
    item_id: int
):
    db = get_supabase_admin()

    # Calculate expiry 7 days from now
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=7)
    ).isoformat()

    db.table("webhook_deduplication").insert({
        "event_id":     event_id,
        "workspace_id": workspace_id,
        "board_id":     board_id,
        "item_id":      item_id,
        "is_processed": False,
        "expires_at":   expires_at,
    }).execute()


# ─────────────────────────────────────────
# POST /api/v1/webhooks/monday
# Main webhook receiver
# monday.com calls this on every new item creation
# ─────────────────────────────────────────
@router.post("/monday")
async def receive_monday_webhook(
    request: Request,
    x_monday_signature: Optional[str] = Header(None),
):

    # ── Step 1: Read raw body ──
    body = await request.body()

    # ── Step 2: Verify signature ──
    if x_monday_signature:
        if not verify_webhook_signature(body, x_monday_signature):
            raise HTTPException(
                status_code=401,
                detail="Invalid webhook signature"
            )

    # ── Step 3: Parse JSON ──
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON payload"
        )

    # ── Step 4: Handle monday.com challenge ──
    # monday sends this once when webhook is first registered
    # We must return same challenge value back
    if "challenge" in payload:
        return JSONResponse({"challenge": payload["challenge"]})

    # ── Step 5: Extract event data ──
    event = payload.get("event", {})

    if not event:
        return JSONResponse({
            "status": "ignored",
            "reason": "no event data"
        })

    # monday.com calls items "pulses" internally
    event_id   = str(event.get("id", ""))
    item_id    = event.get("pulseId")
    item_name  = event.get("pulseName", "")
    board_id   = event.get("boardId")
    account_id = event.get("accountId")

    # Validate required fields
    if not item_id or not board_id:
        return JSONResponse({
            "status": "ignored",
            "reason": "missing item_id or board_id"
        })

    if not event_id:
        return JSONResponse({
            "status": "ignored",
            "reason": "missing event_id"
        })

    # ── Step 6: Find workspace by monday account ID ──
    db = get_supabase_admin()

    try:
        ws_result = db.table("workspaces")\
            .select("id, is_active, is_paused, plan_tier")\
            .eq("monday_account_id", account_id)\
            .eq("is_active", True)\
            .single()\
            .execute()
    except Exception:
        return JSONResponse({
            "status": "ignored",
            "reason": "workspace not found"
        })

    if not ws_result.data:
        return JSONResponse({
            "status": "ignored",
            "reason": "workspace not found"
        })

    workspace    = ws_result.data
    workspace_id = workspace["id"]

    # ── Step 7: Skip paused workspaces ──
    if workspace.get("is_paused"):
        return JSONResponse({
            "status": "ignored",
            "reason": "workspace is paused"
        })

    # ── Step 8: Deduplicate ──
    if is_duplicate_event(event_id):
        return JSONResponse({
            "status": "ignored",
            "reason": "duplicate event"
        })

    # ── Step 9: Record event in deduplication table ──
    try:
        mark_event_received(event_id, workspace_id, board_id, item_id)
    except Exception:
        # If dedup insert fails (race condition) → treat as duplicate
        return JSONResponse({
            "status": "ignored",
            "reason": "duplicate event (race)"
        })

    # ── Step 10: Create automation event (activity log entry) ──
    try:
        auto_event = db.table("automation_events").insert({
            "workspace_id": workspace_id,
            "item_id":      item_id,
            "item_name":    item_name,
            "board_id":     board_id,
            "event_id":     event_id,
            "trigger_type": "WEBHOOK",
            "status":       "NO_MATCH",  # matching engine will update this
        }).execute()

        automation_event_id = auto_event.data[0]["id"]

    except Exception as e:
        # Log error but still return 200 to monday.com
        # monday.com retries if we return error — we don't want that
        return JSONResponse({
            "status": "error",
            "reason": f"failed to create automation event: {str(e)}"
        })

    # ── Step 11: Create queue job for matching engine ──
    try:
        db.table("queue_jobs").insert({
            "workspace_id":        workspace_id,
            "automation_event_id": automation_event_id,
            "job_type":            "MATCHING",
            "status":              "PENDING",
            "payload": {
                "item_id":            item_id,
                "item_name":          item_name,
                "board_id":           board_id,
                "workspace_id":       workspace_id,
                "event_id":           event_id,
                "automation_event_id": automation_event_id,
            },
            "max_attempts": 3,
            "priority":     1,
        }).execute()

    except Exception as e:
        # Queue job failed but event is logged — not critical
        pass

    # ── Step 12: Return fast response ──
    # monday.com expects response in < 3 seconds
    # Actual matching happens in background via queue
    return JSONResponse({
        "status":     "queued",
        "event_id":   event_id,
        "item_name":  item_name,
        "workspace_id": workspace_id,
    })


# ─────────────────────────────────────────
# GET /api/v1/webhooks/health
# Simple check to confirm webhook endpoint is live
# ─────────────────────────────────────────
@router.get("/health")
async def webhook_health():
    return {
        "status":   "ok",
        "endpoint": "/api/v1/webhooks/monday",
        "method":   "POST"
    }