# app/services/copy_engine.py
# ─────────────────────────────────────────────────────────────
# Copy Engine — Copies subitems from template to new item
#
# When a match is found, this engine:
#   1. Reads all subitems from matched template (from DB)
#   2. Creates each subitem under the new item in monday.com
#      via GraphQL API
#   3. Returns how many succeeded and how many failed
#
# Target: complete all copies within 5 seconds of item creation
# ─────────────────────────────────────────────────────────────

import httpx
from app.core.database import get_supabase_admin

# monday.com GraphQL API endpoint
MONDAY_API_URL = "https://api.monday.com/v2"


# ─────────────────────────────────────────
# Create a single subitem in monday.com
# Uses GraphQL mutation
# ─────────────────────────────────────────
async def create_subitem_in_monday(
    parent_item_id: int,
    subitem_name:   str,
    access_token:   str,
) -> dict:
    """
    Create one subitem under a parent item in monday.com.

    Args:
        parent_item_id: The newly created item's ID in monday.com
        subitem_name:   Name of the subitem to create
        access_token:   Workspace OAuth token

    Returns:
        Dict with keys: success (bool), subitem_id (int or None), error (str or None)
    """

    # GraphQL mutation to create a subitem
    mutation = """
        mutation CreateSubitem($parent_id: ID!, $name: String!) {
            create_subitem(
                parent_item_id: $parent_id,
                item_name: $name
            ) {
                id
                name
            }
        }
    """

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                MONDAY_API_URL,
                json={
                    "query":     mutation,
                    "variables": {
                        "parent_id": str(parent_item_id),
                        "name":      subitem_name,
                    }
                },
                headers={
                    "Authorization": access_token,
                    "Content-Type":  "application/json",
                    "API-Version":   "2024-01",
                }
            )

        if response.status_code != 200:
            return {
                "success":    False,
                "subitem_id": None,
                "error":      f"HTTP {response.status_code}: {response.text}"
            }

        data = response.json()

        # Check for GraphQL errors
        if "errors" in data:
            return {
                "success":    False,
                "subitem_id": None,
                "error":      str(data["errors"])
            }

        # Extract created subitem ID
        created = data.get("data", {}).get("create_subitem", {})
        subitem_id = created.get("id")

        if not subitem_id:
            return {
                "success":    False,
                "subitem_id": None,
                "error":      "Subitem created but no ID returned"
            }

        return {
            "success":    True,
            "subitem_id": subitem_id,
            "error":      None
        }

    except httpx.TimeoutException:
        return {
            "success":    False,
            "subitem_id": None,
            "error":      "Request timed out (10s limit)"
        }
    except Exception as e:
        return {
            "success":    False,
            "subitem_id": None,
            "error":      str(e)
        }


# ─────────────────────────────────────────
# Main Copy Function
# Called by worker after a match is found
# ─────────────────────────────────────────
async def copy_subitems(
    template_id:    str,
    item_id:        int,
    access_token:   str,
) -> dict:
    """
    Copy all subitems from a template to a newly created item.

    Flow:
    1. Fetch all subitems from template (ordered by sort_order)
    2. Create each subitem in monday.com via GraphQL
    3. Track which succeeded and which failed
    4. Return summary

    Args:
        template_id:    Template UUID from DB
        item_id:        monday.com ID of the newly created item
        access_token:   Workspace OAuth token

    Returns:
        Dict with:
            subitems_copied:      int — how many succeeded
            subitems_failed:      int — how many failed
            failed_subitem_names: list — names of failed subitems
            success:              bool — True if at least 1 copied
    """

    db = get_supabase_admin()

    # ── Step 1: Fetch template subitems ordered by sort_order ──
    try:
        subitems_result = db.table("template_subitems")\
            .select("*")\
            .eq("template_id", template_id)\
            .is_("deleted_at", "null")\
            .order("sort_order")\
            .execute()

        subitems = subitems_result.data or []
    except Exception as e:
        return {
            "subitems_copied":      0,
            "subitems_failed":      0,
            "failed_subitem_names": [],
            "success":              False,
            "error":                f"Failed to fetch subitems: {str(e)}"
        }

    if not subitems:
        # Template has no subitems — nothing to copy
        return {
            "subitems_copied":      0,
            "subitems_failed":      0,
            "failed_subitem_names": [],
            "success":              True,
            "error":                None
        }

    # ── Step 2: Copy each subitem one by one ──
    # Must maintain order — sort_order is already sorted from DB query
    copied_count        = 0
    failed_count        = 0
    failed_names        = []

    for subitem in subitems:
        subitem_name = subitem.get("name", "")

        if not subitem_name:
            continue

        # Create subitem in monday.com
        result = await create_subitem_in_monday(
            parent_item_id  = item_id,
            subitem_name    = subitem_name,
            access_token    = access_token,
        )

        if result["success"]:
            copied_count += 1
            print(f"[Copy Engine] ✓ Copied subitem: '{subitem_name}'")
        else:
            failed_count += 1
            failed_names.append(subitem_name)
            # Log failure but continue — do not stop for one failure
            print(f"[Copy Engine] ✗ Failed subitem: '{subitem_name}' — {result['error']}")

    # ── Step 3: Update template usage count ──
    try:
        from datetime import datetime, timezone
        db.table("templates")\
            .update({
                "usage_count":  subitems[0].get("usage_count", 0) + 1 if subitems else 1,
                "last_used_at": datetime.now(timezone.utc).isoformat(),
            })\
            .eq("id", template_id)\
            .execute()
    except Exception:
        pass  # Non-critical — don't fail the copy for this

    return {
        "subitems_copied":      copied_count,
        "subitems_failed":      failed_count,
        "failed_subitem_names": failed_names,
        "success":              copied_count > 0,
        "error":                None
    }