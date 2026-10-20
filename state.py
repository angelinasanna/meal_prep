"""Server-side in-memory stores plus session/prefs helpers."""

from fastapi import Request
import asyncio
import secrets
from persistence import load_user_data, save_user_data


# ── Server-side cluster store (avoids cookie size limits) ───────────────────
_cluster_store: dict = {}

_recipe_store: dict = {}  # session_key -> generated recipe content

_cluster_locks: dict = {}  # session_key -> asyncio.Lock (guards concurrent per-meal-type writes)

_plans_store: dict = {}   # session_key -> plans_by_week (kept server-side so cookie stays small)

_persisted_state: dict = {}  # session_key -> last-persisted plan fingerprint, prevents redundant Supabase writes

_macro_store: dict = {}      # "{sk}:{cluster_id}" -> list[dict] per use, or "computing" sentinel


def _get_cluster_lock(sk: str) -> asyncio.Lock:
    if sk not in _cluster_locks:
        _cluster_locks[sk] = asyncio.Lock()
    return _cluster_locks[sk]


def _store_key(request: Request) -> str:
    key = "_cs"
    if key not in request.session:
        request.session[key] = secrets.token_urlsafe(16)
    return request.session[key]


def load_clusters(request: Request) -> list:
    return _cluster_store.get(_store_key(request), [])


def save_clusters(request: Request, clusters: list):
    sk = _store_key(request)
    _cluster_store[sk] = clusters
    # Clear all per-week recipe cache entries for this session
    for k in list(_recipe_store.keys()):
        if k.startswith(sk):
            del _recipe_store[k]


def clear_clusters(request: Request):
    sk = _store_key(request)
    _cluster_store.pop(sk, None)
    _cluster_locks.pop(sk, None)


def get_plans_by_week(request: Request) -> dict:
    sk = _store_key(request)
    if sk in _plans_store:
        return _plans_store[sk]
    # If logged in, try loading from Supabase (handles server restart / eviction)
    user_id = request.session.get("user_id")
    if user_id:
        try:
            data = load_user_data(user_id)
            saved = data.get("plans_by_week", {})
            if saved:
                _plans_store[sk] = saved
                return saved
        except Exception:
            pass
    # Fall back to session (non-logged-in users)
    return request.session.get("plans_by_week", {})


def save_plans_by_week(request: Request, plans: dict):
    sk = _store_key(request)
    _plans_store[sk] = plans
    # Keep a lightweight copy in session for backward-compat reads only
    request.session.pop("plans_by_week", None)
    # Always persist to Supabase immediately for logged-in users
    _persist_user(request)


def get_prefs(request: Request) -> dict:
    return request.session.get("preferences") or {}


def _can_view_plan(clusters: list, **_) -> bool:
    return any(c.get("accepted") for c in clusters)


def _persist_user(request: Request):
    """Save session's user-owned data to their file if they are logged in."""
    user_id = request.session.get("user_id")
    if not user_id:
        return
    data = load_user_data(user_id)
    data["email"] = request.session.get("user_email", data.get("email", ""))
    data["name"] = request.session.get("user_name", data.get("name", ""))
    if "preferences" in request.session:
        data["preferences"] = request.session["preferences"]
    if "cuisinePreferences" in request.session:
        data["cuisinePreferences"] = request.session["cuisinePreferences"]
    sk = _store_key(request)
    if sk in _plans_store:
        data["plans_by_week"] = _plans_store[sk]
    elif "plans_by_week" in request.session:
        data["plans_by_week"] = request.session["plans_by_week"]
    for key, val in list(request.session.items()):
        if key.startswith("checked_"):
            data[key] = val
    save_user_data(user_id, data)
