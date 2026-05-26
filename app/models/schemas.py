# app/models/schemas.py
# ─────────────────────────────────────────────────────────────
# Pydantic models — define shape of every API request + response
# FastAPI uses these to validate input and serialize output
# ─────────────────────────────────────────────────────────────

from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime
from enum import Enum


# ─────────────────────────────────────────
# ENUMS — must match database enum values exactly
# ─────────────────────────────────────────

class AISensitivity(str, Enum):
    STRICT   = "STRICT"
    BALANCED = "BALANCED"
    LOOSE    = "LOOSE"

class MatchMethod(str, Enum):
    AI          = "AI"
    EXACT_MATCH = "EXACT_MATCH"
    FALLBACK    = "FALLBACK"

class EventStatus(str, Enum):
    SUCCESS         = "SUCCESS"
    FAILED          = "FAILED"
    NO_MATCH        = "NO_MATCH"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"

class TemplateSource(str, Enum):
    MANUAL      = "MANUAL"
    AI_BUILDER  = "AI_BUILDER"
    SUGGESTION  = "SUGGESTION"

class SuggestionStatus(str, Enum):
    PENDING   = "PENDING"
    ACCEPTED  = "ACCEPTED"
    DISMISSED = "DISMISSED"


# ─────────────────────────────────────────
# SETTINGS SCHEMAS
# Screen 1 — Settings page
# ─────────────────────────────────────────

# GET /api/settings/{workspace_id} response
class WorkspaceSettingsResponse(BaseModel):
    id:                             str
    workspace_id:                   str
    template_board_id:              Optional[int]   = None
    template_board_name:            Optional[str]   = None
    template_board_deleted:         bool            = False
    ai_sensitivity:                 AISensitivity   = AISensitivity.BALANCED
    ai_enabled:                     bool            = True
    exact_match_fallback_enabled:   bool            = True
    is_enabled:                     bool            = True
    onboarding_completed:           bool            = False

# POST /api/settings/{workspace_id} request body
class WorkspaceSettingsUpdate(BaseModel):
    template_board_id:              Optional[int]           = None
    template_board_name:            Optional[str]           = None
    ai_sensitivity:                 Optional[AISensitivity] = None
    ai_enabled:                     Optional[bool]          = None
    exact_match_fallback_enabled:   Optional[bool]          = None
    is_enabled:                     Optional[bool]          = None


# ─────────────────────────────────────────
# BOARDS SCHEMAS
# Screen 1 — Template board picker dropdown
# ─────────────────────────────────────────

class BoardItem(BaseModel):
    # Single board from monday.com
    id:   str
    name: str

class BoardsListResponse(BaseModel):
    # GET /api/boards/{workspace_id} response
    boards: List[BoardItem]


# ─────────────────────────────────────────
# TEMPLATE SCHEMAS
# Screen 2 — Templates list
# Screen 4 — Template Builder
# ─────────────────────────────────────────

class SubitemResponse(BaseModel):
    # Single subitem inside a template
    id:                         str
    name:                       str
    sort_order:                 int             = 0
    default_status:             Optional[str]   = None
    assigned_monday_user_id:    Optional[int]   = None

class TemplateResponse(BaseModel):
    # Single template row in template list
    id:             str
    name:           str
    description:    Optional[str]   = None
    source:         TemplateSource
    template_type:  Optional[str]   = None
    usage_count:    int             = 0
    last_used_at:   Optional[datetime] = None
    is_active:      bool            = True
    subitem_count:  int             = 0         # Calculated field
    created_at:     datetime

class TemplatesListResponse(BaseModel):
    # GET /api/templates/{workspace_id} response
    templates:  List[TemplateResponse]
    total:      int

class SubitemCreate(BaseModel):
    # Single subitem when creating a template
    name:                       str
    sort_order:                 int             = 0
    default_status:             Optional[str]   = None
    assigned_monday_user_id:    Optional[int]   = None

class TemplateCreate(BaseModel):
    # POST /api/templates/{workspace_id} request body
    name:           str
    description:    Optional[str]   = None
    template_type:  Optional[str]   = None
    subitems:       List[SubitemCreate] = []

class TemplateSubitemsResponse(BaseModel):
    # GET /api/templates/{template_id}/subitems response
    template_id:    str
    template_name:  str
    subitems:       List[SubitemResponse]


# ─────────────────────────────────────────
# AI TEMPLATE BUILDER SCHEMAS
# Screen 4 — Template Builder
# ─────────────────────────────────────────

class GenerateTemplateRequest(BaseModel):
    # POST /api/templates/generate request body
    prompt:         str     # User types: "onboarding template with welcome email, kickoff call..."
    workspace_id:   str

class GeneratedSubitem(BaseModel):
    # Single AI-generated subitem suggestion
    name:           str
    sort_order:     int
    default_status: Optional[str] = None

class GenerateTemplateResponse(BaseModel):
    # POST /api/templates/generate response — shown to user for review
    suggested_name:     str
    suggested_subitems: List[GeneratedSubitem]
    prompt_used:        str

class ConfirmTemplateRequest(BaseModel):
    # POST /api/templates/generate/confirm — user approves AI suggestion
    workspace_id:   str
    name:           str                         # User may have edited the name
    subitems:       List[SubitemCreate]         # User may have edited subitems
    ai_prompt:      str                         # Original prompt — saved for reference


# ─────────────────────────────────────────
# ACTIVITY LOG SCHEMAS
# Screen 3 — Activity Log
# ─────────────────────────────────────────

class ActivityEventResponse(BaseModel):
    # Single row in activity log
    id:                     str
    item_name:              str
    board_name:             Optional[str]       = None
    matched_template_name:  Optional[str]       = None
    match_method:           Optional[MatchMethod] = None
    confidence_score:       Optional[float]     = None
    subitems_copied:        int                 = 0
    subitems_failed:        int                 = 0
    status:                 EventStatus
    ai_fallback_used:       bool                = False
    processing_ms:          Optional[int]       = None
    created_at:             datetime

class ActivityLogResponse(BaseModel):
    # GET /api/activity/{workspace_id} response
    events: List[ActivityEventResponse]
    total:  int

class ActivityEventDetail(BaseModel):
    # GET /api/activity/{event_id}/detail — full event detail
    id:                     str
    item_id:                int
    item_name:              str
    board_id:               Optional[int]       = None
    board_name:             Optional[str]       = None
    matched_template_id:    Optional[str]       = None
    matched_template_name:  Optional[str]       = None
    match_method:           Optional[MatchMethod] = None
    confidence_score:       Optional[float]     = None
    ai_sensitivity_used:    Optional[str]       = None
    trigger_type:           Optional[str]       = None
    subitems_copied:        int                 = 0
    subitems_failed:        int                 = 0
    failed_subitem_names:   Optional[List[str]] = None
    status:                 EventStatus
    error_details:          Optional[str]       = None
    ai_fallback_used:       bool                = False
    retry_count:            int                 = 0
    processing_ms:          Optional[int]       = None
    created_at:             datetime


# ─────────────────────────────────────────
# AI SUGGESTION SCHEMAS
# Screen 1 — AI suggestion banner
# ─────────────────────────────────────────

class SuggestionResponse(BaseModel):
    # Single AI suggestion shown in banner
    id:                     str
    suggested_template_name: str
    detected_item_names:    Optional[List[str]] = None
    occurrence_count:       int
    suggested_subitems:     Optional[List[Any]] = None
    status:                 SuggestionStatus
    confidence_score:       Optional[float]     = None
    created_at:             datetime

class SuggestionAcceptRequest(BaseModel):
    # POST /api/suggestions/{suggestion_id}/accept request body
    workspace_id: str


# ─────────────────────────────────────────
# GENERAL RESPONSE SCHEMAS
# ─────────────────────────────────────────

class SuccessResponse(BaseModel):
    success: bool   = True
    message: str

class ErrorResponse(BaseModel):
    success: bool   = False
    error:   str
    detail:  Optional[str] = None