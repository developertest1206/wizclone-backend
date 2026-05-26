# app/services/matching.py
# ─────────────────────────────────────────────────────────────
# Matching Engine — WizClone's Brain
#
# When a new item is created, this engine finds the best
# matching template using:
#   1. AI matching (monday.com AI Blocks) — if available
#   2. Exact name matching — always available (fallback)
#
# Sensitivity levels control the confidence threshold:
#   STRICT   → 90%+ confidence required
#   BALANCED → 75%+ confidence required
#   LOOSE    → 55%+ confidence required
# ─────────────────────────────────────────────────────────────

from app.core.database import get_supabase_admin
import difflib


# ─────────────────────────────────────────
# Sensitivity → confidence threshold mapping
# If AI confidence is below threshold → no match
# ─────────────────────────────────────────
SENSITIVITY_THRESHOLDS = {
    "STRICT":   0.90,   # 90% — only very close matches
    "BALANCED": 0.75,   # 75% — default, moderate matching
    "LOOSE":    0.55,   # 55% — broader intent matching
}


def get_threshold(sensitivity: str) -> float:
    """Return confidence threshold for given sensitivity level"""
    return SENSITIVITY_THRESHOLDS.get(sensitivity.upper(), 0.75)


# ─────────────────────────────────────────
# Exact Name Matching
# Uses Python difflib — no AI needed
# Always runs as fallback if AI is unavailable
# ─────────────────────────────────────────
def exact_match(
    item_name: str,
    templates: list[dict],
    threshold: float
) -> dict | None:
    """
    Find best matching template using fuzzy string comparison.

    Args:
        item_name:  Name of the newly created item
        templates:  List of template dicts from DB
        threshold:  Minimum similarity score (0.0 to 1.0)

    Returns:
        Best matching template dict, or None if no match found
    """

    if not templates:
        return None

    item_name_lower = item_name.lower().strip()
    best_match      = None
    best_score      = 0.0

    for template in templates:
        template_name = template.get("name", "").lower().strip()

        if not template_name:
            continue

        # difflib gives similarity ratio 0.0 to 1.0
        score = difflib.SequenceMatcher(
            None,
            item_name_lower,
            template_name
        ).ratio()

        # Also check if item name CONTAINS the template name
        # e.g. "Acme Corp Onboarding" contains "onboarding"
        if template_name in item_name_lower:
            score = max(score, 0.80)  # Boost score for substring match

        # Also check matching_keywords if available
        keywords = template.get("matching_keywords", [])
        if isinstance(keywords, list):
            for keyword in keywords:
                if keyword.lower() in item_name_lower:
                    score = max(score, 0.78)  # Boost for keyword match
                    break

        if score > best_score:
            best_score = score
            best_match = template

    # Return match only if score meets threshold
    if best_score >= threshold and best_match:
        return {
            "template":         best_match,
            "confidence_score": round(best_score * 100, 2),  # Convert to percentage
            "match_method":     "EXACT_MATCH",
        }

    return None


# ─────────────────────────────────────────
# AI Matching (monday.com AI Blocks)
# ─────────────────────────────────────────
async def ai_match(
    item_name: str,
    templates: list[dict],
    threshold: float,
    access_token: str
) -> dict | None:
    """
    Find best matching template using monday.com AI Blocks.

    NOTE TO DEVELOPER:
    ─────────────────────────────────────────────────────────
    Replace the TODO block below with actual monday.com AI Blocks call.
    monday.com AI Blocks API is accessed via their SDK or REST API.

    Expected AI Blocks input:
        - item_name: the new item name
        - template_names: list of all template names

    Expected AI Blocks output:
        - best_match_name: which template name matches best
        - confidence: 0-100 score

    Until monday.com AI Blocks is integrated, this function
    falls back to exact matching automatically.
    ─────────────────────────────────────────────────────────

    Args:
        item_name:    Name of newly created item
        templates:    List of template dicts from DB
        threshold:    Minimum confidence threshold
        access_token: Workspace monday.com access token

    Returns:
        Best matching template dict with confidence, or None
    """

    try:
        # ── TODO: Replace this block with monday.com AI Blocks call ──
        #
        # import monday_ai_blocks  # or httpx call to monday.com AI endpoint
        #
        # template_names = [t["name"] for t in templates]
        #
        # ai_result = await monday_ai_blocks.match(
        #     query=item_name,
        #     candidates=template_names,
        #     access_token=access_token
        # )
        #
        # best_name       = ai_result["best_match"]
        # confidence      = ai_result["confidence"]  # 0-100
        #
        # if confidence >= (threshold * 100):
        #     matched_template = next(
        #         (t for t in templates if t["name"] == best_name), None
        #     )
        #     if matched_template:
        #         return {
        #             "template":         matched_template,
        #             "confidence_score": confidence,
        #             "match_method":     "AI",
        #         }
        # return None
        # ── END TODO ──

        # Until AI Blocks is integrated → return None to trigger fallback
        return None

    except Exception as e:
        # AI failed → caller will use exact match fallback
        print(f"[AI Match] AI Blocks error: {e} — falling back to exact match")
        return None


# ─────────────────────────────────────────
# Main Matching Function
# Called by worker for every new item event
# ─────────────────────────────────────────
async def find_best_template(
    workspace_id:   str,
    item_name:      str,
    access_token:   str,
    ai_enabled:     bool,
    sensitivity:    str,
) -> dict:
    """
    Find the best matching template for a new item.

    Flow:
    1. Fetch all active templates for this workspace
    2. Try AI matching (if enabled and available)
    3. If AI fails/unavailable → try exact matching
    4. Return result dict with match info

    Args:
        workspace_id:   Workspace UUID
        item_name:      Name of newly created item
        access_token:   monday.com OAuth token
        ai_enabled:     Whether AI matching is on for this workspace
        sensitivity:    STRICT / BALANCED / LOOSE

    Returns:
        Dict with keys: matched, template, confidence_score,
                        match_method, ai_fallback_used
    """

    db              = get_supabase_admin()
    threshold       = get_threshold(sensitivity)
    ai_fallback_used = False

    # ── Step 1: Fetch all active templates for this workspace ──
    try:
        templates_result = db.table("templates")\
            .select("id, name, matching_keywords")\
            .eq("workspace_id", workspace_id)\
            .eq("is_active", True)\
            .eq("is_deleted", False)\
            .execute()

        templates = templates_result.data or []
    except Exception as e:
        print(f"[Matching] Failed to fetch templates: {e}")
        return _no_match_result()

    if not templates:
        # No templates set up — nothing to match against
        return _no_match_result()

    match_result = None

    # ── Step 2: Try AI matching first (if enabled) ──
    if ai_enabled:
        match_result = await ai_match(
            item_name       = item_name,
            templates       = templates,
            threshold       = threshold,
            access_token    = access_token,
        )

        if match_result:
            match_result["ai_fallback_used"] = False
            return match_result

        # AI returned nothing → fall back to exact match
        ai_fallback_used = True
        print(f"[Matching] AI unavailable for item '{item_name}' — using exact match fallback")

    # ── Step 3: Exact name matching (always runs if AI fails) ──
    match_result = exact_match(
        item_name   = item_name,
        templates   = templates,
        threshold   = threshold,
    )

    if match_result:
        match_result["ai_fallback_used"] = ai_fallback_used
        # If AI was supposed to run but fell back → update method
        if ai_fallback_used:
            match_result["match_method"] = "FALLBACK"
        return match_result

    # ── Step 4: No match found ──
    return _no_match_result()


def _no_match_result() -> dict:
    """Standard no-match response"""
    return {
        "matched":          False,
        "template":         None,
        "confidence_score": 0.0,
        "match_method":     None,
        "ai_fallback_used": False,
    }