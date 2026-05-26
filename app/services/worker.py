# app/services/worker.py
# ─────────────────────────────────────────────────────────────
# Queue Worker — Ties Everything Together
#
# This is the background processor that:
#   1. Picks up PENDING jobs from queue_jobs table
#   2. Runs matching engine to find template
#   3. Runs copy engine to copy subitems
#   4. Updates automation_events with result
#   5. Updates usage_metrics for billing
#
# How to run:
#   python -m app.services.worker
#   (Keep this running alongside uvicorn)
#
# In production: run as a separate process on Railway/Render
# ─────────────────────────────────────────────────────────────

import asyncio
import traceback
from datetime import datetime, timezone

from app.core.database import get_supabase_admin
from app.services.matching import find_best_template
from app.services.copy_engine import copy_subitems


# How often worker checks for new jobs (seconds)
POLL_INTERVAL_SECONDS = 3

# Retry delays (exponential backoff): 1s → 4s → 16s
RETRY_DELAYS = [1, 4, 16]


# ─────────────────────────────────────────
# Process a single MATCHING job
# ─────────────────────────────────────────
async def process_matching_job(job: dict) -> None:
    """
    Main job processor:
    1. Read item info from job payload
    2. Run matching engine
    3. If match found → run copy engine
    4. Update automation_event with result
    5. Mark job as COMPLETED or FAILED
    """

    db          = get_supabase_admin()
    job_id      = job["id"]
    payload     = job.get("payload", {})
    start_time  = datetime.now(timezone.utc)

    # Extract data from job payload
    item_id             = payload.get("item_id")
    item_name           = payload.get("item_name", "")
    board_id            = payload.get("board_id")
    workspace_id        = payload.get("workspace_id")
    automation_event_id = payload.get("automation_event_id")

    print(f"[Worker] Processing job {job_id} — item: '{item_name}'")

    # ── Mark job as RUNNING ──
    db.table("queue_jobs")\
        .update({
            "status":     "RUNNING",
            "started_at": start_time.isoformat(),
        })\
        .eq("id", job_id)\
        .execute()

    try:
        # ── Step 1: Fetch workspace info ──
        ws_result = db.table("workspaces")\
            .select("id, access_token, plan_tier, is_active, is_paused")\
            .eq("id", workspace_id)\
            .single()\
            .execute()

        if not ws_result.data:
            raise Exception(f"Workspace {workspace_id} not found")

        workspace    = ws_result.data
        access_token = workspace["access_token"]

        # Skip if workspace is paused
        if workspace.get("is_paused"):
            print(f"[Worker] Workspace {workspace_id} is paused — skipping")
            await _mark_job_completed(db, job_id)
            return

        # ── Step 2: Fetch workspace settings ──
        settings_result = db.table("workspace_settings")\
            .select("ai_sensitivity, ai_enabled, is_enabled, exact_match_fallback_enabled")\
            .eq("workspace_id", workspace_id)\
            .single()\
            .execute()

        if not settings_result.data:
            raise Exception("Workspace settings not found")

        ws_settings = settings_result.data

        # Skip if automation is disabled
        if not ws_settings.get("is_enabled", True):
            print(f"[Worker] Automation disabled for workspace {workspace_id} — skipping")
            await _mark_job_completed(db, job_id)
            return

        ai_enabled  = ws_settings.get("ai_enabled", True)
        sensitivity = ws_settings.get("ai_sensitivity", "BALANCED")

        # ── Step 3: Run matching engine ──
        print(f"[Worker] Running matching for '{item_name}' (sensitivity: {sensitivity})")

        match_result = await find_best_template(
            workspace_id    = workspace_id,
            item_name       = item_name,
            access_token    = access_token,
            ai_enabled      = ai_enabled,
            sensitivity     = sensitivity,
        )

        # Calculate processing time so far
        now             = datetime.now(timezone.utc)
        processing_ms   = int((now - start_time).total_seconds() * 1000)

        # ── Step 4: No match found ──
        if not match_result.get("matched") or not match_result.get("template"):
            print(f"[Worker] No match found for '{item_name}'")

            # Update automation event — NO_MATCH
            if automation_event_id:
                db.table("automation_events")\
                    .update({
                        "status":           "NO_MATCH",
                        "match_method":     match_result.get("match_method"),
                        "confidence_score": match_result.get("confidence_score", 0),
                        "ai_fallback_used": match_result.get("ai_fallback_used", False),
                        "ai_sensitivity_used": sensitivity,
                        "processing_ms":    processing_ms,
                        "completed_at":     now.isoformat(),
                    })\
                    .eq("id", automation_event_id)\
                    .execute()

            # Update usage metrics
            await _increment_usage(db, workspace_id, "no_match")

            await _mark_job_completed(db, job_id)
            return

        # ── Step 5: Match found → run copy engine ──
        matched_template    = match_result["template"]
        template_id         = matched_template["id"]
        template_name       = matched_template["name"]
        confidence_score    = match_result["confidence_score"]
        match_method        = match_result["match_method"]
        ai_fallback_used    = match_result.get("ai_fallback_used", False)

        print(f"[Worker] Match found: '{template_name}' ({confidence_score}% confidence) — copying subitems")

        # Run copy engine
        copy_result = await copy_subitems(
            template_id     = template_id,
            item_id         = item_id,
            access_token    = access_token,
        )

        # Final processing time
        final_now           = datetime.now(timezone.utc)
        total_processing_ms = int((final_now - start_time).total_seconds() * 1000)

        # Determine final event status
        if copy_result["subitems_failed"] == 0:
            event_status = "SUCCESS"
        elif copy_result["subitems_copied"] > 0:
            event_status = "PARTIAL_SUCCESS"
        else:
            event_status = "FAILED"

        print(f"[Worker] Copy done — {copy_result['subitems_copied']} copied, {copy_result['subitems_failed']} failed — Status: {event_status}")

        # ── Step 6: Update automation event with full result ──
        if automation_event_id:
            db.table("automation_events")\
                .update({
                    "status":                   event_status,
                    "matched_template_id":       template_id,
                    "matched_template_name":     template_name,
                    "match_method":             match_method,
                    "confidence_score":         confidence_score,
                    "ai_sensitivity_used":      sensitivity,
                    "ai_fallback_used":         ai_fallback_used,
                    "subitems_copied":          copy_result["subitems_copied"],
                    "subitems_failed":          copy_result["subitems_failed"],
                    "failed_subitem_names":     copy_result["failed_subitem_names"],
                    "processing_ms":            total_processing_ms,
                    "completed_at":             final_now.isoformat(),
                })\
                .eq("id", automation_event_id)\
                .execute()

        # ── Step 7: Update usage metrics ──
        await _increment_usage(
            db              = db,
            workspace_id    = workspace_id,
            event_type      = "success" if event_status == "SUCCESS" else "partial",
            copies_used     = copy_result["subitems_copied"],
            ai_match_calls  = 1 if match_method == "AI" else 0,
            exact_match     = 1 if match_method == "EXACT_MATCH" else 0,
            fallback        = 1 if ai_fallback_used else 0,
        )

        # Mark job as completed
        await _mark_job_completed(db, job_id)

    except Exception as e:
        # ── Handle job failure ──
        error_msg       = str(e)
        attempt_count   = job.get("attempt_count", 0) + 1
        max_attempts    = job.get("max_attempts", 3)

        print(f"[Worker] Job {job_id} failed (attempt {attempt_count}/{max_attempts}): {error_msg}")
        traceback.print_exc()

        if attempt_count >= max_attempts:
            # Max retries reached → mark as permanently FAILED
            db.table("queue_jobs")\
                .update({
                    "status":        "FAILED",
                    "last_error":    error_msg,
                    "attempt_count": attempt_count,
                    "failed_at":     datetime.now(timezone.utc).isoformat(),
                })\
                .eq("id", job_id)\
                .execute()

            # Update automation event as FAILED
            if automation_event_id:
                db.table("automation_events")\
                    .update({
                        "status":       "FAILED",
                        "error_details": error_msg,
                        "retry_count":  attempt_count,
                    })\
                    .eq("id", automation_event_id)\
                    .execute()
        else:
            # Schedule retry with exponential backoff
            delay           = RETRY_DELAYS[min(attempt_count - 1, len(RETRY_DELAYS) - 1)]
            next_retry_at   = datetime.now(timezone.utc)

            from datetime import timedelta
            next_retry_at = (next_retry_at + timedelta(seconds=delay)).isoformat()

            db.table("queue_jobs")\
                .update({
                    "status":        "PENDING",   # Reset to PENDING for retry
                    "last_error":    error_msg,
                    "attempt_count": attempt_count,
                    "next_retry_at": next_retry_at,
                })\
                .eq("id", job_id)\
                .execute()

            print(f"[Worker] Retry scheduled in {delay}s for job {job_id}")


# ─────────────────────────────────────────
# Helper — Mark job as COMPLETED
# ─────────────────────────────────────────
async def _mark_job_completed(db, job_id: str) -> None:
    """Mark a queue job as successfully completed"""
    db.table("queue_jobs")\
        .update({
            "status":       "COMPLETED",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })\
        .eq("id", job_id)\
        .execute()


# ─────────────────────────────────────────
# Helper — Update Usage Metrics
# Tracks monthly usage for plan enforcement
# ─────────────────────────────────────────
async def _increment_usage(
    db,
    workspace_id:   str,
    event_type:     str     = "success",
    copies_used:    int     = 0,
    ai_match_calls: int     = 0,
    exact_match:    int     = 0,
    fallback:       int     = 0,
) -> None:
    """
    Update usage_metrics for current billing cycle.
    Creates a new row if this is the first event this month.
    """
    try:
        # Get current billing cycle start (first day of month)
        now             = datetime.now(timezone.utc)
        cycle_start     = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        cycle_end       = (cycle_start.replace(month=cycle_start.month % 12 + 1)
                          if cycle_start.month < 12
                          else cycle_start.replace(year=cycle_start.year + 1, month=1))

        # Check if row exists for this billing cycle
        existing = db.table("usage_metrics")\
            .select("id, copies_used, ai_match_calls, exact_match_count, fallback_match_count, no_match_count, total_automation_runs, success_runs, failed_runs")\
            .eq("workspace_id", workspace_id)\
            .eq("billing_cycle_start", cycle_start.date().isoformat())\
            .execute()

        if existing.data:
            # Update existing row
            row         = existing.data[0]
            update_data = {
                "copies_used":          row["copies_used"] + copies_used,
                "ai_match_calls":       row["ai_match_calls"] + ai_match_calls,
                "exact_match_count":    row["exact_match_count"] + exact_match,
                "fallback_match_count": row["fallback_match_count"] + fallback,
                "total_automation_runs": row["total_automation_runs"] + 1,
                "last_copy_at":         now.isoformat(),
            }

            if event_type == "no_match":
                update_data["no_match_count"] = row["no_match_count"] + 1
            elif event_type == "success":
                update_data["success_runs"] = row["success_runs"] + 1
            else:
                update_data["failed_runs"] = row["failed_runs"] + 1

            db.table("usage_metrics")\
                .update(update_data)\
                .eq("id", row["id"])\
                .execute()
        else:
            # Insert new row for this billing cycle
            db.table("usage_metrics").insert({
                "workspace_id":         workspace_id,
                "billing_cycle_start":  cycle_start.date().isoformat(),
                "billing_cycle_end":    cycle_end.date().isoformat(),
                "copies_used":          copies_used,
                "ai_match_calls":       ai_match_calls,
                "exact_match_count":    exact_match,
                "fallback_match_count": fallback,
                "no_match_count":       1 if event_type == "no_match" else 0,
                "total_automation_runs": 1,
                "success_runs":         1 if event_type == "success" else 0,
                "failed_runs":          1 if event_type not in ["success", "no_match"] else 0,
                "last_copy_at":         now.isoformat(),
            }).execute()

    except Exception as e:
        # Non-critical — don't fail the job for metrics error
        print(f"[Worker] Usage metrics update failed: {e}")


# ─────────────────────────────────────────
# Main Worker Loop
# Polls queue_jobs table every 3 seconds
# ─────────────────────────────────────────
async def run_worker() -> None:
    """
    Continuous background worker loop.
    Polls DB every 3 seconds for PENDING jobs.
    Processes them one by one.
    """
    print("[Worker] 🚀 WizClone background worker started")
    print(f"[Worker] Polling every {POLL_INTERVAL_SECONDS} seconds")

    db = get_supabase_admin()

    while True:
        try:
            # Fetch PENDING jobs that are ready to run
            # next_retry_at = NULL means ready immediately
            now = datetime.now(timezone.utc).isoformat()

            jobs_result = db.table("queue_jobs")\
                .select("*")\
                .eq("status", "PENDING")\
                .eq("job_type", "MATCHING")\
                .or_(f"next_retry_at.is.null,next_retry_at.lte.{now}")\
                .order("priority", desc=True)\
                .order("created_at")\
                .limit(5)\
                .execute()

            jobs = jobs_result.data or []

            if jobs:
                print(f"[Worker] Found {len(jobs)} pending job(s)")
                # Process jobs concurrently (max 5 at once)
                await asyncio.gather(*[
                    process_matching_job(job)
                    for job in jobs
                ])
            else:
                # No jobs — wait before polling again
                pass

        except Exception as e:
            print(f"[Worker] Poll error: {e}")
            traceback.print_exc()

        # Wait before next poll
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ─────────────────────────────────────────
# Entry point
# Run with: python -m app.services.worker
# ─────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run_worker())