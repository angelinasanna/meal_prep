"""Supabase-backed per-user data storage."""

from dotenv import load_dotenv
from supabase import Client as SupabaseClient
from supabase import create_client
import hashlib
import os

load_dotenv()


# ── User data persistence (Supabase) ─────────────────────────────────────────
_supabase: SupabaseClient = create_client(
    os.getenv("SUPABASE_URL", ""),
    os.getenv("SUPABASE_KEY", ""),
)


def _user_id(email: str) -> str:
    return hashlib.md5(email.lower().strip().encode()).hexdigest()


def load_user_data(user_id: str) -> dict:
    try:
        res = _supabase.table("user_data").select("*").eq("user_id", user_id).single().execute()
        row = res.data or {}
        return {**row.get("data", {}), "name": row.get("name", ""), "email": row.get("email", "")}
    except Exception:
        return {}


def save_user_data(user_id: str, data: dict):
    try:
        payload = {
            "user_id": user_id,
            "email": data.pop("email", ""),
            "name": data.pop("name", ""),
            "data": data,
            "updated_at": "now()",
        }
        _supabase.table("user_data").upsert(payload).execute()
    except Exception:
        pass
