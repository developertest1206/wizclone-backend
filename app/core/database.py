# app/core/database.py
# Supabase client setup
# Two clients: anon (normal) and admin (bypass RLS)

from supabase import create_client, Client
from app.core.config import settings


# ── Anon client ──
# Use for normal read/write operations
# Respects Row Level Security (RLS)
supabase: Client = create_client(
    settings.supabase_url,
    settings.supabase_anon_key
)


# ── Admin client ──
# Use for backend operations (webhooks, billing, plan enforcement)
# Bypasses RLS — use carefully
supabase_admin: Client = create_client(
    settings.supabase_url,
    settings.supabase_service_role_key
)


def get_supabase() -> Client:
    """Return normal supabase client"""
    return supabase


def get_supabase_admin() -> Client:
    """Return admin supabase client (bypasses RLS)"""
    return supabase_admin