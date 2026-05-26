# main.py
# ─────────────────────────────────────────────────────────────
# WizClone FastAPI Backend — Entry Point
# All routes are registered here
# Run with: uvicorn main:app --reload --port 8000
# ─────────────────────────────────────────────────────────────

from fastapi import FastAPI

# Import CORS middleware, which controls who is allowed to talk to your server
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings

# Import all route modules
from app.api import auth, webhooks, settings_routes, templates, activity, suggestions, boards

# ── Create FastAPI app ──
app = FastAPI(
    title="WizClone API",
    description="Smart Template & Subitem Automation for monday.com",
    version="1.0.0",
    # Swagger UI only visible in development
    docs_url="/docs" if settings.app_env == "development" else None,
    redoc_url="/redoc" if settings.app_env == "development" else None,
)


# ── CORS Middleware ──
# Allows monday.com app panel (frontend) to call this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # Restrict in production to monday.com domain
    allow_credentials=True,     # Allow sending cookies or login information along with requests
    allow_methods=["*"],        # Allow all types of requests like GET, POST, PUT, DELETE
    allow_headers=["*"],        # Allow all kinds of headers (extra information sent with requests)
)

# ─────────────────────────────────────────────────────────────
# Register all API routes
# ─────────────────────────────────────────────────────────────

# Auth — OAuth install, callback, me, uninstall
app.include_router(auth.router,             prefix="/api/v1/auth",        tags=["Auth"])

# Webhook — monday.com item create events
app.include_router(webhooks.router,         prefix="/api/v1/webhooks",    tags=["Webhooks"])

# Settings — workspace settings CRUD + boards list
app.include_router(settings_routes.router,  prefix="/api/v1",             tags=["Settings"])

# Boards — list monday.com boards for template board picker
app.include_router(boards.router,           prefix="/api/v1",             tags=["Boards"])

# Templates — CRUD + subitems
app.include_router(templates.router,        prefix="/api/v1",             tags=["Templates"])

# Activity Log — automation events history
app.include_router(activity.router,         prefix="/api/v1",             tags=["Activity"])

# AI Suggestions — accept / dismiss
app.include_router(suggestions.router,      prefix="/api/v1",             tags=["Suggestions"])


# ─────────────────────────────────────────────────────────────
# Base endpoints
# ─────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
async def root():
    # Root endpoint — confirms API is running
    return {
        "message": "WizClone API is running",
        "version": "1.0.0",
        "docs":    "/docs"
    }


@app.get("/health", tags=["Health"])
async def health_check():
    # Health check — used by Railway/Render to confirm app is alive
    return {
        "status": "ok",
        "env":    settings.app_env
    }