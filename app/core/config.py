# app/core/config.py
# ─────────────────────────────────────────────────────────────
# All environment variables loaded from .env file
# Import `settings` anywhere in the app to use them
# ─────────────────────────────────────────────────────────────

from pydantic_settings import BaseSettings


class Settings(BaseSettings):

    # ── Supabase ──
    supabase_url:               str           # Project URL from Supabase dashboard
    supabase_anon_key:          str           # Public anon key
    supabase_service_role_key:  str           # Admin key (backend only — never expose)
    database_url:               str           # PostgreSQL connection string

    # ── JWT ──
    jwt_secret_key:                     str = "change-this-in-production"
    jwt_algorithm:                      str = "HS256"
    jwt_access_token_expire_minutes:    int = 60

    # ── App ──
    app_env:        str = "development"       # development / production
    app_port:       int = 8000
    app_base_url:   str = "http://localhost:8000"  # Change on Railway/Render deploy

    # ── monday.com ──
    monday_client_id:       str = ""          # From monday Developer Center
    monday_client_secret:   str = ""
    monday_webhook_secret:  str = ""          # Used to verify webhook signatures
    monday_api_token:       str = ""          # Personal API token (optional)
    app_id:                 str = ""          # monday.com App ID

    class Config:
        env_file         = ".env"
        env_file_encoding = "utf-8"
        extra            = "ignore"           # Ignore any unknown keys in .env


# Global settings object — import this everywhere
settings = Settings()