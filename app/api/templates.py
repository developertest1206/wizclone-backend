# app/api/templates.py
# ─────────────────────────────────────────────────────────────
# Screen 2 — Templates List
# Screen 4 — AI Template Builder
#
# Routes:
#   GET    /api/v1/templates/{workspace_id}          → list all templates
#   POST   /api/v1/templates/{workspace_id}          → create new template
#   DELETE /api/v1/templates/{template_id}           → soft delete template
#   GET    /api/v1/templates/{template_id}/subitems  → get template subitems
#   POST   /api/v1/templates/generate                → AI generate subitems from prompt
#   POST   /api/v1/templates/generate/confirm        → save AI-generated template
# ─────────────────────────────────────────────────────────────

from fastapi import APIRouter, HTTPException, Depends, Query
from app.core.database import get_supabase_admin
from app.core.dependencies import get_current_workspace
from app.models.schemas import (
    TemplateResponse, TemplatesListResponse,
    TemplateCreate, TemplateSubitemsResponse, SubitemResponse,
    GenerateTemplateRequest, GenerateTemplateResponse,
    ConfirmTemplateRequest, GeneratedSubitem, SuccessResponse
)
from typing import Optional
import httpx

router = APIRouter()

MONDAY_API_URL = "https://api.monday.com/v2"


# ─────────────────────────────────────────
# GET /api/v1/templates/{workspace_id}
# Screen 2 — load templates list
# Shows: name, subitem count, copy count, source
# ─────────────────────────────────────────
@router.get(
    "/templates/{workspace_id}",
    response_model=TemplatesListResponse
)
async def list_templates(
    workspace_id: str,
    search: Optional[str] = Query(None),        # Optional search filter
    workspace: dict = Depends(get_current_workspace)
):
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()

    # Fetch all active (not deleted) templates for this workspace
    query = db.table("templates")\
        .select("*")\
        .eq("workspace_id", workspace_id)\
        .eq("is_deleted", False)\
        .eq("is_active", True)\
        .order("created_at", desc=True)

    result = query.execute()
    templates_data = result.data or []

    # Apply search filter if provided
    if search:
        search_lower = search.lower()
        templates_data = [
            t for t in templates_data
            if search_lower in t["name"].lower()
        ]

    # For each template, count subitems
    templates = []
    for t in templates_data:
        # Count subitems for this template
        subitem_count_result = db.table("template_subitems")\
            .select("id", count="exact")\
            .eq("template_id", t["id"])\
            .is_("deleted_at", "null")\
            .execute()

        subitem_count = subitem_count_result.count or 0

        templates.append(TemplateResponse(
            id              = t["id"],
            name            = t["name"],
            description     = t.get("description"),
            source          = t.get("source", "MANUAL"),
            template_type   = t.get("template_type"),
            usage_count     = t.get("usage_count", 0),
            last_used_at    = t.get("last_used_at"),
            is_active       = t.get("is_active", True),
            subitem_count   = subitem_count,
            created_at      = t["created_at"],
        ))

    return TemplatesListResponse(
        templates   = templates,
        total       = len(templates)
    )


# ─────────────────────────────────────────
# POST /api/v1/templates/{workspace_id}
# Screen 2 — create new template manually
# Also used by AI builder confirm step
# ─────────────────────────────────────────
@router.post(
    "/templates/{workspace_id}",
    response_model=TemplateResponse
)
async def create_template(
    workspace_id: str,
    body: TemplateCreate,
    workspace: dict = Depends(get_current_workspace)
):
    if workspace["id"] != workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()

    # Check plan limit — Free plan: max 1 template, Pro: max 10
    plan_tier = workspace.get("plan_tier", "FREE")
    if plan_tier in ["FREE", "PRO"]:
        max_templates = 1 if plan_tier == "FREE" else 10

        count_result = db.table("templates")\
            .select("id", count="exact")\
            .eq("workspace_id", workspace_id)\
            .eq("is_deleted", False)\
            .execute()

        current_count = count_result.count or 0

        if current_count >= max_templates:
            raise HTTPException(
                status_code=403,
                detail=f"{plan_tier} plan allows max {max_templates} template(s). Please upgrade."
            )

    # Insert template into DB
    try:
        template_result = db.table("templates").insert({
            "workspace_id":     workspace_id,
            "name":             body.name,
            "description":      body.description,
            "template_type":    body.template_type,
            "source":           "MANUAL",
            "created_by_ai":    False,
            "is_active":        True,
            "is_deleted":       False,
            "usage_count":      0,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create template: {str(e)}")

    template = template_result.data[0]
    template_id = template["id"]

    # Insert all subitems for this template
    if body.subitems:
        subitems_to_insert = [
            {
                "template_id":              template_id,
                "name":                     s.name,
                "sort_order":               s.sort_order,
                "default_status":           s.default_status,
                "assigned_monday_user_id":  s.assigned_monday_user_id,
            }
            for s in body.subitems
        ]
        try:
            db.table("template_subitems").insert(subitems_to_insert).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Template created but subitems failed: {str(e)}")

    return TemplateResponse(
        id              = template["id"],
        name            = template["name"],
        description     = template.get("description"),
        source          = template.get("source", "MANUAL"),
        template_type   = template.get("template_type"),
        usage_count     = 0,
        is_active       = True,
        subitem_count   = len(body.subitems),
        created_at      = template["created_at"],
    )


# ─────────────────────────────────────────
# DELETE /api/v1/templates/{template_id}
# Screen 2 — delete a template (soft delete)
# Sets is_deleted=True — does not remove from DB
# ─────────────────────────────────────────
@router.delete(
    "/templates/{template_id}",
    response_model=SuccessResponse
)
async def delete_template(
    template_id: str,
    workspace: dict = Depends(get_current_workspace)
):
    db = get_supabase_admin()

    # Verify this template belongs to the current workspace
    try:
        check = db.table("templates")\
            .select("id, workspace_id")\
            .eq("id", template_id)\
            .single()\
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Template not found")

    if not check.data:
        raise HTTPException(status_code=404, detail="Template not found")

    if check.data["workspace_id"] != workspace["id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # Soft delete — keep record in DB for audit
    from datetime import datetime, timezone
    db.table("templates")\
        .update({
            "is_deleted": True,
            "is_active":  False,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        })\
        .eq("id", template_id)\
        .execute()

    return SuccessResponse(message="Template deleted successfully")


# ─────────────────────────────────────────
# GET /api/v1/templates/{template_id}/subitems
# Screen 2 — click on template row to see its subitems
# ─────────────────────────────────────────
@router.get(
    "/templates/{template_id}/subitems",
    response_model=TemplateSubitemsResponse
)
async def get_template_subitems(
    template_id: str,
    workspace: dict = Depends(get_current_workspace)
):
    db = get_supabase_admin()

    # Fetch template and verify ownership
    try:
        template_result = db.table("templates")\
            .select("id, name, workspace_id")\
            .eq("id", template_id)\
            .eq("is_deleted", False)\
            .single()\
            .execute()
    except Exception:
        raise HTTPException(status_code=404, detail="Template not found")

    if not template_result.data:
        raise HTTPException(status_code=404, detail="Template not found")

    if template_result.data["workspace_id"] != workspace["id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # Fetch subitems ordered by sort_order
    subitems_result = db.table("template_subitems")\
        .select("*")\
        .eq("template_id", template_id)\
        .is_("deleted_at", "null")\
        .order("sort_order")\
        .execute()

    subitems = [
        SubitemResponse(
            id                      = s["id"],
            name                    = s["name"],
            sort_order              = s.get("sort_order", 0),
            default_status          = s.get("default_status"),
            assigned_monday_user_id = s.get("assigned_monday_user_id"),
        )
        for s in (subitems_result.data or [])
    ]

    return TemplateSubitemsResponse(
        template_id     = template_id,
        template_name   = template_result.data["name"],
        subitems        = subitems
    )


# ─────────────────────────────────────────
# POST /api/v1/templates/generate
# Screen 4 — AI Template Builder
# User types a prompt → AI generates subitem suggestions
# Uses monday.com AI Blocks (mocked here — developer integrates AI Blocks)
# ─────────────────────────────────────────
@router.post(
    "/templates/generate",
    response_model=GenerateTemplateResponse
)
async def generate_template(
    body: GenerateTemplateRequest,
    workspace: dict = Depends(get_current_workspace)
):
    if workspace["id"] != body.workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Check plan — AI Builder is Business plan only
    if workspace.get("plan_tier") != "BUSINESS":
        raise HTTPException(
            status_code=403,
            detail="AI Template Builder requires Business plan"
        )

    # ── monday.com AI Blocks Integration Point ──
    # TODO: Developer replaces this block with actual monday.com AI Blocks call
    # The prompt is sent to monday.com AI Blocks which returns structured subitems
    #
    # Example monday.com AI Blocks call:
    # result = monday_ai_blocks.generate(
    #     prompt=body.prompt,
    #     output_format="subitem_list"
    # )
    #
    # For now — rule-based extraction as placeholder
    # Real implementation: send body.prompt to monday.com AI Blocks

    prompt_lower = body.prompt.lower()

    # Extract keywords from prompt to build subitem suggestions
    # This is a simple placeholder — monday.com AI Blocks will do this intelligently
    suggested_subitems = _extract_subitems_from_prompt(body.prompt)

    # Generate a template name from prompt (first meaningful phrase)
    suggested_name = _extract_template_name(body.prompt)

    return GenerateTemplateResponse(
        suggested_name      = suggested_name,
        suggested_subitems  = suggested_subitems,
        prompt_used         = body.prompt,
    )


def _extract_template_name(prompt: str) -> str:
    """
    Extract a short template name from user's prompt.
    Monday.com AI Blocks will handle this intelligently.
    This is a placeholder.
    """
    # Take first 50 chars as name, clean up
    name = prompt.strip()
    if len(name) > 50:
        # Cut at last space before 50 chars
        name = name[:50].rsplit(" ", 1)[0]
    return name.title()


def _extract_subitems_from_prompt(prompt: str) -> list[GeneratedSubitem]:
    """
    Parse prompt for task names separated by commas, 'and', or 'with'.
    Placeholder — monday.com AI Blocks replaces this.
    """
    import re

    # Split on comma, 'and', 'with', semicolon
    parts = re.split(r",|and|with|;", prompt, flags=re.IGNORECASE)

    subitems = []
    order = 1

    for part in parts:
        clean = part.strip()

        # Skip very short fragments or filler words
        if len(clean) < 4:
            continue

        # Skip the instruction part (before first verb)
        skip_words = ["create a template for", "make a template", "template for",
                      "steps for", "tasks for", "generate"]
        should_skip = any(clean.lower().startswith(sw) for sw in skip_words)
        if should_skip:
            # Try to extract the subject after the filler
            for sw in skip_words:
                if clean.lower().startswith(sw):
                    clean = clean[len(sw):].strip()
                    break

        if len(clean) < 3:
            continue

        subitems.append(GeneratedSubitem(
            name            = clean.strip().capitalize(),
            sort_order      = order,
            default_status  = None,
        ))
        order += 1

    return subitems


# ─────────────────────────────────────────
# POST /api/v1/templates/generate/confirm
# Screen 4 — User reviewed and confirmed AI-generated template
# Saves it to DB as a real template
# ─────────────────────────────────────────
@router.post(
    "/templates/generate/confirm",
    response_model=TemplateResponse
)
async def confirm_generated_template(
    body: ConfirmTemplateRequest,
    workspace: dict = Depends(get_current_workspace)
):
    if workspace["id"] != body.workspace_id:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_supabase_admin()

    # Insert template with source = AI_BUILDER
    try:
        template_result = db.table("templates").insert({
            "workspace_id":     body.workspace_id,
            "name":             body.name,
            "source":           "AI_BUILDER",
            "created_by_ai":    True,
            "ai_prompt":        body.ai_prompt,
            "is_active":        True,
            "is_deleted":       False,
            "usage_count":      0,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save template: {str(e)}")

    template    = template_result.data[0]
    template_id = template["id"]

    # Insert subitems
    if body.subitems:
        subitems_to_insert = [
            {
                "template_id":  template_id,
                "name":         s.name,
                "sort_order":   s.sort_order,
                "default_status": s.default_status,
            }
            for s in body.subitems
        ]
        try:
            db.table("template_subitems").insert(subitems_to_insert).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Subitems save failed: {str(e)}")

    return TemplateResponse(
        id              = template["id"],
        name            = template["name"],
        description     = None,
        source          = "AI_BUILDER",
        usage_count     = 0,
        is_active       = True,
        subitem_count   = len(body.subitems),
        created_at      = template["created_at"],
    )