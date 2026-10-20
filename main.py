from fastapi import FastAPI, Request, Form, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
import json
import math
import os
import random
import re
import uuid
from datetime import date, timedelta
from typing import List
from dotenv import load_dotenv

from llm import _gemini
from persistence import _user_id, load_user_data, save_user_data
from state import _can_view_plan, _get_cluster_lock, _macro_store, _persist_user, _persisted_state, _plans_store, _recipe_store, _store_key, clear_clusters, get_plans_by_week, get_prefs, load_clusters, save_clusters, save_plans_by_week
from nutrition import USDA_API_KEY, _bg_compute_macros
from calendar_utils import DAYS_SHORT, _MONTH_NAMES, _build_calendar, _month_weeks, _scale_amount_f, _week_dates, _week_key
from recipe_content import CUISINES, SEASONS, _SEASON_GUIDANCE, _content_words, _infer_vegetarian, _normalize_ingredient, _recipe_name_context
from recipe_index import _build_recipe_catalog, _estimate_recipe_calories, _semantic_recipe_ids
from grocery import _build_grocery_by_recipe, _build_grocery_data, _categorize_dessert_ingredient, _parse_dessert_ingredient, _raw_ing_to_structured
from recipe_gen import _generate_recipes
from recipe_data import RECIPE_DB, _DESSERT_CUISINE, _RECIPE_CUISINE, _RECIPE_MEAL_TYPES

load_dotenv()


app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "mealprepped-dev-secret-change-in-prod"),
)

# ── Google OAuth ─────────────────────────────────────────────────────────────
oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)
app.mount("/static", StaticFiles(directory="static"), name="static")
import time as _time
templates = Jinja2Templates(directory="templates")
templates.env.globals["css_v"] = str(int(_time.time()))


# ── Routes ──────────────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/landing", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def do_login(request: Request, email: str = Form(...), name: str = Form("")):
    uid = _user_id(email)
    data = load_user_data(uid)
    request.session["user_id"] = uid
    request.session["user_email"] = email.lower().strip()
    request.session["user_name"] = name.strip() or data.get("name") or email.split("@")[0]
    # Restore saved data, keeping any newer session data on top
    if "preferences" in data and "preferences" not in request.session:
        request.session["preferences"] = data["preferences"]
    if "cuisinePreferences" in data and "cuisinePreferences" not in request.session:
        request.session["cuisinePreferences"] = data["cuisinePreferences"]
    if "plans_by_week" in data:
        sk = _store_key(request)
        existing = _plans_store.get(sk, {})
        _plans_store[sk] = {**data["plans_by_week"], **existing}
    for key, val in data.items():
        if key.startswith("checked_") and key not in request.session:
            request.session[key] = val
    # Save updated name
    data["name"] = request.session["user_name"]
    data["email"] = request.session["user_email"]
    save_user_data(uid, data)
    return RedirectResponse("/landing", status_code=302)


@app.post("/logout")
async def do_logout(request: Request):
    _persist_user(request)
    sk = _store_key(request)
    _plans_store.pop(sk, None)
    _persisted_state.pop(sk, None)
    request.session.clear()
    return RedirectResponse("/landing", status_code=302)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(url="/landing", status_code=302)


@app.get("/landing", response_class=HTMLResponse)
async def landing_page(request: Request):
    prefs = request.session.get("preferences")
    has_prefs = bool(prefs)

    _pool = [r for r in RECIPE_DB if r.get("image")]
    _carousel_sample = random.sample(_pool, min(5, len(_pool)))
    carousel_recipes = [
        {"id": r["_id"], "name": r["base"]["title"], "caption": r.get("intro", ""), "image": r.get("image", "")}
        for r in _carousel_sample
    ]

    # Always compute week context so calendar + dates show even without prefs
    today = date.today()
    current_week = _week_key(today)
    selected_week = request.session.get("selectedWeek", current_week)
    try:
        week_dates_list = _week_dates(selected_week)
    except Exception:
        selected_week = current_week
        week_dates_list = _week_dates(selected_week)

    cal_year = int(request.session.get("calYear", week_dates_list[0].year))
    cal_month = int(request.session.get("calMonth", week_dates_list[0].month))
    month_weeks = _month_weeks(cal_year, cal_month)

    mon0 = week_dates_list[0]
    sun0 = week_dates_list[6]
    if mon0.month == sun0.month:
        week_range_base = f"{mon0.strftime('%b')} {mon0.day}–{sun0.day}, {mon0.year}"
    else:
        week_range_base = f"{mon0.strftime('%b')} {mon0.day} – {sun0.strftime('%b')} {sun0.day}, {mon0.year}"

    max_future = _week_key(today + timedelta(weeks=8))
    prev_week_key_base = _week_key(mon0 - timedelta(weeks=1))
    next_cand = _week_key(mon0 + timedelta(weeks=1))
    next_week_key_base = next_cand if next_cand <= max_future else None

    ctx: dict = {
        "request": request,
        "has_prefs": has_prefs,
        "has_plan": False,
        "carousel_recipes": carousel_recipes,
        "preferences": prefs or {},
        "calendar_data": [],
        "week_range": week_range_base,
        "selected_week": selected_week,
        "current_week": current_week,
        "cal_year": cal_year,
        "cal_month": cal_month,
        "cal_month_name": _MONTH_NAMES[cal_month - 1],
        "month_weeks": month_weeks,
        "prev_year": cal_year if cal_month > 1 else cal_year - 1,
        "prev_month": cal_month - 1 if cal_month > 1 else 12,
        "next_year": cal_year if cal_month < 12 else cal_year + 1,
        "next_month": cal_month + 1 if cal_month < 12 else 1,
        "today_str": today.isoformat(),
        "today": today,
        "prev_week_key": prev_week_key_base,
        "next_week_key": next_week_key_base,
    }

    if has_prefs:

        plans_by_week: dict = get_plans_by_week(request)
        week_plan = plans_by_week.get(selected_week)
        accepted = week_plan.get("clusters", []) if week_plan else []
        wk_planner = week_plan.get("planner", {}) if week_plan else {}
        meal_prep_days = prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"])
        day_completions = request.session.get("dayCompletions", {})

        calendar_data = []
        if accepted or wk_planner:
            if wk_planner:
                cal, _ = _build_calendar(planner=wk_planner)
            else:
                cal, _ = _build_calendar(accepted, meal_prep_days)
            day_name_to_date = {DAYS_SHORT[d.weekday()]: d for d in week_dates_list}
            for cal_day in cal:
                d = day_name_to_date.get(cal_day["short"])
                date_str = d.isoformat() if d else None
                calendar_data.append({
                    "short": cal_day["short"],
                    "full": cal_day["full"],
                    "date": d,
                    "date_str": date_str,
                    "meals": cal_day.get("meals", {}),
                    "meal_count": len(cal_day.get("meals", {})),
                    "is_complete": bool(day_completions.get(date_str)) if date_str else False,
                    "is_today": d == today if d else False,
                    "is_past": (d < today) if d else False,
                })

        _db_map_landing = {r["_id"]: r for r in RECIPE_DB}
        dessert_ids: list = week_plan.get("desserts", list(request.session.get("desserts", []))) if week_plan else []
        dessert_recipes_landing = [_db_map_landing[rid] for rid in dessert_ids if rid in _db_map_landing]

        _DOW_COLORS = ["#E07A5F", "#F2CC8F", "#81B29A", "#6D9DC5", "#A78BCA", "#E0956A", "#D4A5A5"]
        today_color = _DOW_COLORS[today.weekday()]
        ctx.update({
            "calendar_data": calendar_data,
            "has_plan": bool(week_plan),
            "dessert_recipes": dessert_recipes_landing,
            "today_color": today_color,
        })

    return templates.TemplateResponse("landing.html", ctx)


@app.get("/update-plan")
async def update_plan(request: Request):
    """Un-skip skipped clusters so user can edit their plan on /suggest."""
    if not request.session.get("preferences"):
        return RedirectResponse("/")
    clusters = load_clusters(request)
    for c in clusters:
        if c.get("skipped"):
            c["skipped"] = False
    save_clusters(request, clusters)
    return RedirectResponse("/suggest", status_code=302)


@app.get("/profile", response_class=HTMLResponse)
async def profile_page():
    return RedirectResponse("/preferences", status_code=302)


@app.post("/hide-recipe/{recipe_id}", response_class=HTMLResponse)
async def hide_recipe(request: Request, recipe_id: str):
    prefs = request.session.get("preferences", {})
    hidden: list = prefs.get("hidden_recipes", [])
    if recipe_id not in hidden:
        hidden.append(recipe_id)
    prefs["hidden_recipes"] = hidden
    request.session["preferences"] = prefs
    _persist_user(request)
    return HTMLResponse("")


@app.post("/unhide-recipe/{recipe_id}", response_class=HTMLResponse)
async def unhide_recipe(request: Request, recipe_id: str):
    prefs = request.session.get("preferences", {})
    hidden: list = prefs.get("hidden_recipes", [])
    prefs["hidden_recipes"] = [h for h in hidden if h != recipe_id]
    request.session["preferences"] = prefs
    _persist_user(request)
    return HTMLResponse("")


@app.get("/preferences", response_class=HTMLResponse)
async def preferences_page(request: Request):
    prefs = request.session.get("preferences")
    if not prefs:
        return RedirectResponse("/setup")
    hidden_ids: list = prefs.get("hidden_recipes", [])
    _db_map = {r["_id"]: r for r in RECIPE_DB}
    hidden_recipes = [
        {"id": rid, "title": _db_map[rid]["base"]["title"], "image": _db_map[rid].get("image", "")}
        for rid in hidden_ids if rid in _db_map
    ]
    return templates.TemplateResponse("preferences.html", {
        "request": request,
        "preferences": prefs,
        "hidden_recipes": hidden_recipes,
    })


@app.post("/save-preferences")
async def save_preferences(
    request: Request,
    servings: int = Form(2),
    dietary_needs: str = Form(""),
    adventure_level: str = Form("curious"),
    meal_prep_days: List[str] = Form(default=[]),
    shopping_frequency: str = Form("weekly"),
):
    if not meal_prep_days:
        meal_prep_days = request.session.get("preferences", {}).get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"])
    if shopping_frequency not in ("weekly", "biweekly"):
        shopping_frequency = "weekly"
    existing = request.session.get("preferences", {})
    request.session["preferences"] = {
        **existing,
        "servings": servings,
        "dietaryNeeds": dietary_needs.strip(),
        "adventureLevel": adventure_level,
        "mealPrepDays": meal_prep_days,
        "shoppingFrequency": shopping_frequency,
    }
    _persist_user(request)
    return RedirectResponse("/preferences", status_code=303)


@app.post("/sync-nutrition-goal")
async def sync_nutrition_goal(request: Request):
    body = await request.json()
    calories = int(body.get("calories", 0))
    if calories >= 500:
        existing = request.session.get("preferences", {})
        existing["calorieGoal"] = calories
        request.session["preferences"] = existing
        _persist_user(request)
    return JSONResponse({"ok": True})


@app.get("/auth/google")
async def google_login(request: Request):
    redirect_uri = str(request.url_for("google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback", name="google_callback")
async def google_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
        user = token.get("userinfo") or {}
        email = (user.get("email") or "").lower().strip()
        name = user.get("name") or email.split("@")[0]
        if not email:
            return RedirectResponse("/login?error=1", status_code=302)
        uid = _user_id(email)
        data = load_user_data(uid)
        request.session["user_id"] = uid
        request.session["user_email"] = email
        request.session["user_name"] = name
        if "preferences" in data and "preferences" not in request.session:
            request.session["preferences"] = data["preferences"]
        if "cuisinePreferences" in data and "cuisinePreferences" not in request.session:
            request.session["cuisinePreferences"] = data["cuisinePreferences"]
        if "plans_by_week" in data:
            sk = _store_key(request)
            existing = _plans_store.get(sk, {})
            _plans_store[sk] = {**data["plans_by_week"], **existing}
        data["name"] = name
        data["email"] = email
        save_user_data(uid, data)
    except Exception:
        return RedirectResponse("/login?error=1", status_code=302)
    return RedirectResponse("/landing", status_code=302)


@app.get("/setup", response_class=HTMLResponse)
async def onboarding(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "preferences": {}})


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_redirect(request: Request):
    return RedirectResponse("/setup", status_code=301)


@app.get("/check-session")
async def check_session(request: Request):
    prefs = request.session.get("preferences")
    clusters = load_clusters(request)
    planner = request.session.get("planner", {})
    plans_by_week = get_plans_by_week(request)
    has_saved_plans = any(
        p.get("clusters") for p in plans_by_week.values()
    )
    has_activity = bool(clusters) or any(bool(v) for v in planner.values()) or has_saved_plans
    if not prefs and not has_activity:
        return JSONResponse({"has_session": False})
    if not has_activity:
        return JSONResponse({"has_session": False})
    return JSONResponse({"has_session": True, "continue_url": "/suggest"})


@app.post("/clear-session")
async def clear_session_route(request: Request):
    for key in ["preferences", "clusters", "planner", "desserts", "weekDays",
                "mode", "cuisinePreferences", "existingIngredients"]:
        request.session.pop(key, None)
    sk = _store_key(request)
    _plans_store.pop(sk, None)
    _persisted_state.pop(sk, None)
    user_id = request.session.get("user_id")
    if user_id:
        try:
            data = load_user_data(user_id)
            data["plans_by_week"] = {}
            save_user_data(user_id, data)
        except Exception:
            pass
    return JSONResponse({"ok": True})


@app.post("/setup")
async def setup(
    request: Request,
    servings: int = Form(2),
    dietary_needs: str = Form(""),
    budget_level: str = Form("moderate"),
    meal_prep_days: List[str] = Form(default=[]),
    mode: str = Form("suggestions"),
):
    if not meal_prep_days:
        meal_prep_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    request.session["preferences"] = {
        "servings": servings,
        "dietaryNeeds": dietary_needs.strip(),
        "budgetLevel": budget_level,
        "mealPrepDays": meal_prep_days,
    }
    request.session["mode"] = mode
    _persist_user(request)
    if mode == "existing":
        return RedirectResponse("/ingredients", status_code=303)
    return RedirectResponse("/cuisine", status_code=303)


@app.post("/update-settings")
async def update_settings(
    request: Request,
    servings: int = Form(2),
    dietary_needs: str = Form(""),
    budget_level: str = Form("moderate"),
    meal_prep_days: List[str] = Form(default=[]),
):
    if not meal_prep_days:
        meal_prep_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    request.session["preferences"] = {
        "servings": servings,
        "dietaryNeeds": dietary_needs.strip(),
        "budgetLevel": budget_level,
        "mealPrepDays": meal_prep_days,
    }
    _persist_user(request)
    clear_clusters(request)
    return RedirectResponse("/cuisine", status_code=303)


@app.get("/mode", response_class=HTMLResponse)
async def mode_page(request: Request):
    if not request.session.get("preferences"):
        return RedirectResponse("/")
    return templates.TemplateResponse("mode.html", {"request": request, "preferences": get_prefs(request)})


@app.post("/mode")
async def set_mode(request: Request, mode: str = Form(...)):
    request.session["mode"] = mode
    clear_clusters(request)
    request.session["checkedGrocery"] = []
    if mode == "existing":
        return RedirectResponse("/ingredients", status_code=303)
    return RedirectResponse("/cuisine", status_code=303)


@app.get("/cuisine", response_class=HTMLResponse)
async def cuisine_page(request: Request):
    if not request.session.get("preferences"):
        return RedirectResponse("/")
    return templates.TemplateResponse("cuisine.html", {
        "request": request,
        "preferences": get_prefs(request),
        "cuisines": CUISINES,
        "seasons": SEASONS,
        "selected_cuisines": [],
        "selected_season": "",
    })


@app.post("/cuisine")
async def save_cuisine(
    request: Request,
    cuisines: List[str] = Form(default=[]),
    season: str = Form(""),
):
    request.session["cuisinePreferences"] = {"cuisines": cuisines, "season": season}
    clear_clusters(request)
    _persist_user(request)
    return RedirectResponse("/suggest", status_code=303)


@app.get("/ingredients", response_class=HTMLResponse)
async def ingredients_page(request: Request):
    if not request.session.get("preferences"):
        return RedirectResponse("/")
    raw = request.session.get("existingIngredients", [])
    # Normalise legacy string format to dict list
    existing: list = []
    for item in raw:
        if isinstance(item, dict):
            existing.append(item)
        else:
            m = re.match(r'^(.+?)\s*\(([^)]+)\)\s*$', str(item))
            if m:
                existing.append({"name": m.group(1).strip(), "amount": m.group(2).strip()})
            else:
                existing.append({"name": str(item).strip(), "amount": ""})
    return templates.TemplateResponse("ingredients.html", {
        "request": request,
        "preferences": get_prefs(request),
        "existing_ingredients": existing,
    })


def _ingredients_changed(old: list, new: list) -> bool:
    """True if the ingredient lists differ (order-insensitive, case-insensitive)."""
    def key(lst):
        return sorted((i.get("name", "").lower().strip(), i.get("amount", "").lower().strip()) for i in lst)
    return key(old) != key(new)


@app.post("/ingredients")
async def save_ingredients(
    request: Request,
    ing_name: List[str] = Form(default=[]),
    ing_amount: List[str] = Form(default=[]),
):
    items = []
    for name, amount in zip(ing_name, ing_amount):
        name = name.strip()
        amount = amount.strip()
        if name:
            items.append({"name": name, "amount": amount})

    old_raw = request.session.get("existingIngredients", [])
    # Normalise legacy string format for comparison
    old: list = []
    for item in old_raw:
        if isinstance(item, dict):
            old.append(item)
        else:
            m = re.match(r'^(.+?)\s*\(([^)]+)\)\s*$', str(item))
            old.append({"name": m.group(1).strip(), "amount": m.group(2).strip()} if m else {"name": str(item).strip(), "amount": ""})

    request.session["existingIngredients"] = items
    if _ingredients_changed(old, items):
        clear_clusters(request)
    return RedirectResponse("/suggest", status_code=303)


@app.get("/suggest", response_class=HTMLResponse)
async def suggest_page(request: Request):
    if not request.session.get("preferences"):
        return RedirectResponse("/")
    clusters = load_clusters(request)
    prefs = request.session["preferences"]
    week_days: list = request.session.get("weekDays", prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"]))
    mode = request.session.get("mode", "suggestions")
    existing = request.session.get("existingIngredients", [])
    has_clusters = any(not c.get("reserve") and not c.get("skipped") for c in clusters)
    planner: dict = request.session.get("planner", {})
    planned_count = sum(len(slots) for slots in planner.values())
    selected_desserts: list = list(request.session.get("desserts", []))
    cp = request.session.get("cuisinePreferences", {})
    selected_cuisines_for_dessert: list = cp.get("cuisines", [])
    all_dessert_recipes = [
        {
            "id": r["_id"],
            "title": r["base"]["title"],
            "image": r.get("image", ""),
            "cuisine": _DESSERT_CUISINE.get(r["_id"], ""),
            "selected": r["_id"] in selected_desserts,
        }
        for r in RECIPE_DB
        if "dessert" in _RECIPE_MEAL_TYPES.get(r["_id"], [])
    ]
    # Filter by selected cuisines; fall back to all if none match
    if selected_cuisines_for_dessert:
        filtered = [d for d in all_dessert_recipes if d["cuisine"] in selected_cuisines_for_dessert]
        dessert_recipes = filtered if filtered else all_dessert_recipes
    else:
        dessert_recipes = all_dessert_recipes
    return templates.TemplateResponse(
        "suggest.html",
        {
            "request": request,
            "preferences": prefs,
            "clusters": clusters,
            "has_clusters": has_clusters,
            "can_view_plan": _can_view_plan(clusters),
            "mode": mode,
            "existing_ingredients": existing,
            "week_days": week_days,
            "planner": planner,
            "planned_count": planned_count,
            "dessert_recipes": dessert_recipes,
            "selected_desserts": selected_desserts,
        },
    )


# ── Calorie estimation ────────────────────────────────────────────────────────


# ── Recipe vector index (TF-IDF for semantic ingredient search) ───────────────


@app.post("/generate", response_class=HTMLResponse)
async def generate_suggestions(request: Request):
    prefs = request.session.get("preferences")
    if not prefs:
        return HTMLResponse("<p class='error-msg'>Session expired. <a href='/'>Start over</a></p>")

    # Clear the planner and dessert selections when new suggestions are generated
    request.session.pop("planner", None)
    request.session["desserts"] = []

    servings: int = prefs.get("servings", 2)
    dietary_needs: str = prefs.get("dietaryNeeds", "")
    budget_level: str = prefs.get("budgetLevel", "moderate")
    week_days: list = request.session.get("weekDays", prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"]))
    mode: str = request.session.get("mode", "suggestions")

    n_days = len(week_days) if week_days else 5
    n_shown = 6
    n_reserve = 2
    n_total = n_shown + n_reserve
    meals_per_cluster = max(2, math.ceil(n_days / 2))

    raw_existing = request.session.get("existingIngredients", [])
    existing: list = []
    for item in raw_existing:
        if isinstance(item, dict):
            existing.append(item)
        else:
            m = re.match(r'^(.+?)\s*\(([^)]+)\)\s*$', str(item))
            if m:
                existing.append({"name": m.group(1).strip(), "amount": m.group(2).strip()})
            else:
                existing.append({"name": str(item).strip(), "amount": ""})

    if mode == "existing" and _infer_vegetarian(existing):
        if not dietary_needs:
            dietary_needs = "vegetarian"
        elif "vegetarian" not in dietary_needs.lower():
            dietary_needs += ", vegetarian"

    existing_note = ""
    if existing:
        lines = "\n".join(f"  - {i['name']}: {i['amount']}" for i in existing if i.get("name"))
        existing_note = f"""
The user already has these ingredients:
{lines}
- Set "userHas": true if they have enough; false with only the deficit amount if partial; false with full amount if none.
- Prioritise recipes that use what they already have.\
"""

    cuisine_note = ""
    selected_cuisines = []
    if mode == "suggestions":
        cp = request.session.get("cuisinePreferences", {})
        selected_cuisines = cp.get("cuisines", [])
        season = cp.get("season", "")
        if selected_cuisines:
            cuisine_note += f"\nCuisine style: {', '.join(selected_cuisines)}."
        if season and season in _SEASON_GUIDANCE:
            cuisine_note += f"\nSeason: {season}. Lean into {_SEASON_GUIDANCE[season]}"

    try:
        _budget_num = float(str(budget_level).replace("$", "").replace(",", "").strip())
        adventure_note = f"\nBudget: approximately ${_budget_num:.0f}/week — keep ingredient costs in mind and note which extras are optional."
    except (ValueError, TypeError):
        if budget_level == "budget":
            adventure_note = "\nBudget: keep ingredients minimal and affordable, skip optional garnishes, specialty items, and pricier add-ons."
        elif budget_level == "flexible":
            adventure_note = "\nBudget: flexible, feel free to include specialty ingredients, premium proteins, and optional extras."
        else:
            adventure_note = "\nBudget: moderate, include all core ingredients but note which extras are optional."

    adventure_level = prefs.get("adventureLevel", "curious")
    if adventure_level == "familiar":
        adventure_note += " Stick to approachable, recognizable dishes the user likely already knows."
    elif adventure_level == "bold":
        adventure_note += " Be adventurous, include bold flavors, global ingredients, and less common techniques."

    favorite_ids_pref: list = prefs.get("favorited_recipes", [])
    fav_titles = [r["base"]["title"] for r in RECIPE_DB if r["_id"] in favorite_ids_pref]
    if fav_titles:
        if budget_level == "budget":
            favorites_note = (
                f"\nFavorites: The user has saved these recipes, prioritize them heavily: {', '.join(fav_titles)}."
            )
        else:
            favorites_note = (
                f"\nFavorites: Try to include at least one of these saved recipes: {', '.join(fav_titles)}."
            )
    else:
        favorites_note = ""

    _hidden_ids = get_prefs(request).get("hidden_recipes", [])
    _catalog_cuisines = selected_cuisines if mode == "suggestions" else []
    _allowed_ids: set | None = None

    if mode == "existing" and existing:
        # Build a query from the user's ingredients and dietary context
        ing_names = " ".join(i["name"] for i in existing if i.get("name"))
        _semantic_query = f"{ing_names} {dietary_needs}".strip()
        _allowed_ids = set(_semantic_recipe_ids(_semantic_query, n=35, exclude_ids=set(_hidden_ids)))
    elif not _catalog_cuisines:
        # No cuisine filter and not ingredient-based: cap at 80 for token budget
        pass

    _catalog_max = None if (_catalog_cuisines or _allowed_ids) else 50
    catalog = _build_recipe_catalog(
        dietary_needs, _catalog_cuisines, _hidden_ids,
        max_recipes=_catalog_max, allowed_ids=_allowed_ids,
    )

    system_prompt = (
        "You are a meal planning assistant. Select recipes ONLY from the provided catalog. "
        "Do NOT generate ingredients — they are pulled from the database automatically. "
        "Return ONLY valid JSON, no markdown fences, no explanation."
    )

    user_prompt = f"""Create a weekly meal plan by selecting recipes from the catalog below.

RECIPE CATALOG (format: [recipe_id] Title [cuisine] (meal_type Nsrv) NNNcal  Variation1 | Variation2 | Variation3):
{catalog}

User:
- Servings: {servings} people
- Dietary restrictions: {dietary_needs or 'none'}{adventure_note}
- Meal prep days: {', '.join(week_days)}
{existing_note}{cuisine_note}{favorites_note}

Rules:
- Suggest exactly {n_total} clusters ({n_shown} displayed + {n_reserve} hidden backups)
- CRITICAL: recipe_id MUST be an exact id from the catalog above. Do NOT invent ids.
- variation names MUST be exact strings from the catalog (the part after the ). Do NOT invent variation names.
- Each cluster uses ONE recipe_id. Pick {meals_per_cluster} variations from that recipe for the meals array.
- BREAKFAST SEPARATION: exactly 2 of the first {n_shown} clusters must be breakfast recipes (meal_type "breakfast"). Set clusterCategory "breakfast". The other {n_shown - 2} shown clusters must be lunch/dinner. Set clusterCategory "main".
- Clusters 1–2: breakfast recipes. Clusters 3–{n_shown}: lunch/dinner. Make the 2 breakfast clusters use DIFFERENT recipe_ids.
- No two clusters may share the same recipe_id.
- suggestedDay: one abbreviation from the meal prep days ({', '.join(week_days)}), spread across different days
- calories: use the NNNcal figure from the catalog as the base. Adjust per meal variation: if a variation removes rice/bread show fewer calories, if it adds bread/noodles show more. Each meal in the meals array gets its own calories value. Give a specific integer per meal.
Return this exact JSON (no markdown):
{{
  "clusters": [
    {{
      "id": "short_unique_id",
      "recipe_id": "exact-id-from-catalog",
      "clusterCategory": "breakfast",
      "meals": [{{"variation": "Exact Variation Name From Catalog", "mealType": "breakfast", "suggestedDay": "Mon", "calories": 400}}],
      "difficulty": "Easy"
    }}
  ]
}}"""

    try:
        raw = await _gemini(user_prompt, system=system_prompt)
        if not raw:
            raise ValueError("No text block in response")
        json_match = re.search(r"\{[\s\S]*\}", raw)
        if not json_match:
            raise ValueError("No JSON found in response")

        json_str = re.sub(r",\s*([}\]])", r"\1", json_match.group(0))
        data = json.loads(json_str)
        new_clusters = data.get("clusters", [])

        _db_lookup = {r["_id"]: r for r in RECIPE_DB}

        # Drop clusters whose recipe_id isn't in the database; hidden backups then move up
        valid_clusters = [c for c in new_clusters if c.get("recipe_id") in _db_lookup]
        if new_clusters and not valid_clusters:
            raise ValueError("Model returned no recipe_ids from the catalog")
        new_clusters = valid_clusters

        for i, cluster in enumerate(new_clusters):
            cluster["accepted"] = False
            cluster["skipped"] = False
            cluster["reserve"] = (i >= n_shown)
            if not cluster.get("id"):
                cluster["id"] = str(uuid.uuid4())[:8]

            # Expand from DB using recipe_id
            recipe_id = cluster.get("recipe_id")
            db_rec = _db_lookup.get(recipe_id) if recipe_id else None
            if db_rec:
                cluster["clusterName"] = db_rec["base"]["title"]
                intro = db_rec.get("intro", "")
                cluster["tagline"] = re.split(r"[\[\]|]", intro)[0].strip()[:100]
                cluster["image"] = db_rec.get("image", "")
                uses_by_name = {u["name"]: u for u in db_rec.get("uses", [])}
                recipe_title = db_rec["base"]["title"]
                db_calories = _estimate_recipe_calories(db_rec)
                # Build variation display names.
                # If NO distinctive recipe words appear in the variation, prepend context:
                #   "with" recipe  → "{Turmeric Salmon} and {Crispy Potato Bowl}"
                #                    "{Pistachio Salmon} and {Butternut Squash Bowl}"
                #   plain recipe   → "{Lemon Bean} {Pita Pocket Vegetable Salad}"
                # Overlap check uses _VAR_STOP-filtered stems so generic words
                # (vegetable, salad, bowl…) don't mask a missing key ingredient.
                _r_content = {w.lower().rstrip("s") for w in _content_words(recipe_title)}
                _ctx = _recipe_name_context(recipe_title)
                for meal in cluster.get("meals", []):
                    var = meal.get("variation", "")
                    _v_stem = {w.lower().rstrip("s") for w in re.split(r"[\s\-/&,]+", var) if len(w) > 2}
                    if var and not (_r_content & _v_stem):
                        if _ctx["with_key"]:
                            meal["name"] = f"{_ctx['with_key']} and {var}"
                        elif _ctx["full_ctx"]:
                            meal["name"] = f"{_ctx['full_ctx']} {var}"
                        else:
                            meal["name"] = var
                    else:
                        meal["name"] = var
                    meal["calories"] = db_calories
                    use = uses_by_name.get(var, {})
                    if use.get("subtitle") and not meal.get("description"):
                        meal["description"] = use["subtitle"]
            elif not cluster.get("clusterName"):
                cluster["clusterName"] = cluster.get("id", "Meal Set")

            # Replace AI-generated ingredients with exact RECIPE_DB ingredients
            if db_rec:
                db_srv = max(db_rec.get("servings", 4), 1)
                n_meals_c = max(len(cluster.get("meals", [])), 1)
                base_scale = (servings * n_meals_c) / db_srv
                structured: list = []
                for raw in db_rec["base"].get("ingredients", []):
                    item = _raw_ing_to_structured(raw, base_scale)
                    if item:
                        structured.append(item)
                # Include extras for each selected variation (at per-serving scale)
                uses_by_name = {u["name"]: u for u in db_rec.get("uses", [])}
                seen_extra_keys: set = set()
                for meal in cluster.get("meals", []):
                    var = meal.get("variation", "")
                    use = uses_by_name.get(var, {})
                    extra_scale = servings  # extras are per-use per-serving
                    for raw in use.get("extras", []):
                        key = raw.lower().strip() if isinstance(raw, str) else ""
                        if not key or key in seen_extra_keys:
                            continue
                        seen_extra_keys.add(key)
                        item = _raw_ing_to_structured(raw, extra_scale)
                        if item:
                            structured.append(item)
                cluster["ingredients"] = structured

            if not cluster.get("clusterCategory"):
                meal_types = [m.get("mealType", "") for m in cluster.get("meals", [])]
                cluster["clusterCategory"] = "breakfast" if meal_types and all(t == "breakfast" for t in meal_types) else "main"

        sk = _store_key(request)
        async with _get_cluster_lock(sk):
            save_clusters(request, new_clusters)

        visible = [c for c in new_clusters if not c.get("reserve")]
        week_days_ctx = request.session.get("weekDays", prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"]))
        return templates.TemplateResponse(
            "partials/clusters_list.html",
            {
                "request": request,
                "clusters": visible,
                "mode": mode,
                "week_days": week_days_ctx,
            },
        )

    except Exception as exc:
        return templates.TemplateResponse(
            "partials/generate_error.html",
            {"request": request, "error": str(exc)},
        )


def _cluster_card_ctx(request: Request, cluster: dict) -> dict:
    prefs = get_prefs(request)
    week_days = request.session.get("weekDays", prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"]))
    return {
        "request": request,
        "cluster": cluster,
        "mode": request.session.get("mode", "suggestions"),
        "week_days": week_days,
    }


@app.post("/toggle/{cluster_id}", response_class=HTMLResponse)
async def toggle_cluster(request: Request, cluster_id: str):
    sk = _store_key(request)
    target = None
    async with _get_cluster_lock(sk):
        clusters = load_clusters(request)
        target = next((c for c in clusters if c["id"] == cluster_id), None)
        if target:
            target["accepted"] = not target.get("accepted", False)
            save_clusters(request, clusters)

    if not target:
        return HTMLResponse(
            f'<div id="cluster-{cluster_id}" class="cluster-card" '
            f'style="padding:20px;text-align:center;">'
            f'<p style="color:var(--text-3);font-size:.875rem;margin-bottom:12px;">Session expired.</p>'
            f'<a href="/suggest" class="btn btn-primary btn-sm">Reload suggestions</a>'
            f'</div>'
        )

    return templates.TemplateResponse("partials/cluster_card.html", _cluster_card_ctx(request, target))


@app.post("/skip/{cluster_id}", response_class=HTMLResponse)
async def skip_cluster(request: Request, cluster_id: str):
    sk = _store_key(request)
    replacement = None
    async with _get_cluster_lock(sk):
        clusters = load_clusters(request)
        target = next((c for c in clusters if c["id"] == cluster_id), None)
        if not target:
            return HTMLResponse(f'<div id="cluster-{cluster_id}" style="display:none"></div>')

        target["skipped"] = True

        replacement = next(
            (c for c in clusters if c.get("reserve") and not c.get("skipped")),
            None
        )
        if replacement:
            replacement["reserve"] = False
        else:
            replacement = next(
                (c for c in clusters if
                 c.get("skipped") and not c.get("accepted") and c["id"] != cluster_id),
                None
            )
            if replacement:
                replacement["skipped"] = False
                replacement["reserve"] = False

        save_clusters(request, clusters)

    if replacement:
        return templates.TemplateResponse("partials/cluster_card.html", _cluster_card_ctx(request, replacement))
    return HTMLResponse(
        f'<div id="cluster-{cluster_id}" style="display:none;height:0;margin:0;padding:0;overflow:hidden;"></div>'
    )


@app.post("/cluster/{cluster_id}/meal-type", response_class=HTMLResponse)
async def change_meal_type(
    request: Request,
    cluster_id: str,
    meal_name: str = Form(...),
    next_type: str = Form(...),
):
    _valid = {"breakfast", "lunch", "dinner"}
    if next_type not in _valid:
        next_type = "lunch"
    clusters = load_clusters(request)
    for c in clusters:
        if c["id"] == cluster_id:
            for m in c.get("meals", []):
                if m.get("name") == meal_name:
                    m["mealType"] = next_type
            meal_types = list({m.get("mealType", "dinner") for m in c.get("meals", [])})
            if len(meal_types) == 1:
                c["clusterCategory"] = meal_types[0]
            save_clusters(request, clusters)
            return templates.TemplateResponse(
                "partials/cluster_card.html", _cluster_card_ctx(request, c)
            )
    return HTMLResponse(f'<div id="cluster-{cluster_id}"></div>')


@app.post("/assign-meal")
async def assign_meal(
    request: Request,
    cluster_id: str = Form(...),
    meal_index: int = Form(...),
    day: str = Form(""),
    meal_slot: str = Form(""),
):
    sk = _store_key(request)
    async with _get_cluster_lock(sk):
        clusters = load_clusters(request)
        target = next((c for c in clusters if c["id"] == cluster_id), None)
        if target and 0 <= meal_index < len(target.get("meals", [])):
            target["meals"][meal_index]["dayAssignment"] = {"day": day, "meal_slot": meal_slot}
            save_clusters(request, clusters)
    return HTMLResponse("")


@app.get("/grocery", response_class=HTMLResponse)
async def plan_page(request: Request, background_tasks: BackgroundTasks, week: str = None):
    today = date.today()
    current_week = _week_key(today)
    prefs = get_prefs(request)
    week_days: list = prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"])
    plans_by_week: dict = get_plans_by_week(request)

    # Save current session clusters into their designated week only
    clusters = load_clusters(request)
    accepted_now = [c for c in clusters if c.get("accepted")]
    selected_week_session = request.session.get("selectedWeek", current_week)
    if accepted_now:
        planner: dict = request.session.get("planner", {})
        desserts = sorted(request.session.get("desserts", []))
        new_cluster_ids = sorted(c.get("_id", c.get("base", {}).get("title", "")) for c in accepted_now)
        plans_by_week[selected_week_session] = {
            "clusters": accepted_now,
            "meal_prep_days": week_days,
            "planner": planner,
            "desserts": desserts,
        }
        save_plans_by_week(request, plans_by_week)
        _pfx = _store_key(request)
        for _k in list(_recipe_store.keys()):
            if _k.startswith(_pfx): del _recipe_store[_k]
        sk = _store_key(request)
        last = _persisted_state.get(sk, {})
        if (last.get("week") != selected_week_session
                or last.get("cluster_ids") != new_cluster_ids
                or last.get("planner") != planner
                or last.get("desserts") != desserts):
            _persist_user(request)
            _persisted_state[sk] = {"week": selected_week_session, "cluster_ids": new_cluster_ids,
                                    "planner": planner, "desserts": desserts}

    # Determine which week to display — honour ?week= exactly, no cross-week fallback
    if week and re.match(r"^\d{4}-W\d{2}$", week):
        view_week = week
        request.session["selectedWeek"] = week
    else:
        view_week = selected_week_session

    def _fmt_week(wk: str) -> str:
        try:
            dates = _week_dates(wk)
            return f"{dates[0].strftime('%b %-d')} – {dates[-1].strftime('%-d')}"
        except Exception:
            return wk

    # Build week nav: all weeks with plans + current + 8 weeks ahead (empty or not)
    plan_week_keys = set(plans_by_week.keys())
    future_week_keys = {_week_key(today + timedelta(weeks=i)) for i in range(0, 9)}
    all_weeks = sorted(plan_week_keys | future_week_keys, reverse=True)
    view_week_label = _fmt_week(view_week)
    week_nav = [{"key": wk, "label": _fmt_week(wk), "active": wk == view_week, "has_plan": wk in plans_by_week} for wk in all_weeks]

    week_plan = plans_by_week.get(view_week)
    if not week_plan:
        return templates.TemplateResponse("plan.html", {
            "request": request,
            "empty": True,
            "view_week": view_week,
            "view_week_label": view_week_label,
            "week_nav": week_nav,
            "current_week": current_week,
        })

    accepted = week_plan.get("clusters", [])
    wk_days = week_plan.get("meal_prep_days", week_days)
    wk_planner = week_plan.get("planner", {})

    if not accepted:
        return templates.TemplateResponse("plan.html", {
            "request": request,
            "empty": True,
            "view_week": view_week,
            "view_week_label": view_week_label,
            "week_nav": week_nav,
            "current_week": current_week,
        })

    checked: List[str] = request.session.get(f"checked_{view_week}", [])
    calendar, _ = _build_calendar(planner=wk_planner) if wk_planner else _build_calendar(accepted)
    raw_items, grocery_by_category = _build_grocery_data(accepted, wk_days, planner=wk_planner)
    grocery_by_recipe = _build_grocery_by_recipe(accepted)

    _db_map = {r["_id"]: r for r in RECIPE_DB}

    # USDA macro store key (same prefix as recipes page)
    sk_mac = f"{_store_key(request)}:{view_week}"

    # Trigger USDA computation for any cluster not yet computed
    if USDA_API_KEY:
        for ci, c in enumerate(accepted):
            mkey = f"{sk_mac}:{ci}"
            if mkey not in _macro_store:
                _macro_store[mkey] = "computing"
                background_tasks.add_task(_bg_compute_macros, mkey, c, int(get_prefs(request).get("servings", 2)))

    # Build meal_calories from USDA data; fall back to DB/estimated calories_per_serving
    meal_calories: dict = {}
    for ci, c in enumerate(accepted):
        mkey = f"{sk_mac}:{ci}"
        usda_result = _macro_store.get(mkey)
        db_rec = _db_map.get(c.get("recipe_id", ""))
        db_cal = _estimate_recipe_calories(db_rec) if db_rec else 0
        for ui, m in enumerate(c.get("meals", [])):
            if not m.get("name"):
                continue
            if isinstance(usda_result, list) and ui < len(usda_result):
                cal = usda_result[ui].get("calories", 0)
                if cal:
                    meal_calories[m["name"]] = cal
                    continue
            if db_cal:
                meal_calories[m["name"]] = db_cal

    # ── Portion scaling to match calorie goal ─────────────────────────────────
    calorie_goal: int = int(prefs.get("calorieGoal", 0))
    portion_scale: float = 1.0
    if calorie_goal >= 500 and meal_calories:
        # Use calendar day totals for avg; each day must have at least 2 meals worth of calories
        day_totals = []
        for day in calendar:
            day_cal = sum(meal_calories.get(m_name, 0) for m_name in day.get("meals", {}).values())
            n_slots = len(day.get("meals", {}))
            if day_cal > 0 and n_slots >= 2:
                day_totals.append(day_cal)
        # Fallback: sum all unique meal calories and divide by n_days
        if not day_totals:
            total_cal = sum(meal_calories.values())
            n_days = max(len(wk_days), 1)
            day_totals = [total_cal / n_days] if total_cal else []
        if day_totals:
            # Scale so the highest-calorie day lands at or just under the goal.
            # Using max (not avg) guarantees no day ever goes over.
            max_daily_cal = max(day_totals)
            raw_scale = calorie_goal / max_daily_cal
            portion_scale = min(max(raw_scale, 0.25), 4.0)
            # Scale grocery ingredient amounts
            if abs(portion_scale - 1.0) > 0.02:
                for item in raw_items:
                    scaled = _scale_amount_f(str(item.get("amount", "")), portion_scale)
                    # Count-based items (no unit) must stay whole numbers
                    unit = (item.get("unit") or "").strip()
                    if not unit:
                        try:
                            scaled = str(max(1, round(float(scaled))))
                        except (ValueError, TypeError):
                            pass
                    item["amount"] = scaled
            # Scale displayed meal calories
            meal_calories = {k: int(round(v * portion_scale)) for k, v in meal_calories.items()}

    meal_labels = {"breakfast": "Breakfast", "lunch": "Lunch", "dinner": "Dinner"}

    # Build prep checklist from accepted clusters that have prep_tasks in DB
    prep_checklist = []
    for c in accepted:
        db_rec = _db_map.get(c.get("recipe_id", ""))
        tasks = db_rec.get("prep_tasks") if db_rec else None
        if tasks:
            prep_checklist.append({"name": c.get("clusterName", db_rec["base"]["title"]), "tasks": tasks})

    # Load desserts for this week
    dessert_ids: list = week_plan.get("desserts", list(request.session.get("desserts", [])))
    dessert_recipes = [_db_map[rid] for rid in dessert_ids if rid in _db_map]

    # Add dessert ingredients, merging into existing items where possible
    if dessert_recipes:
        raw_items_map: dict = {item["key"]: item for item in raw_items}
        seen_dessert: set = set()
        for dr in dessert_recipes:
            recipe_title = dr.get("base", {}).get("title", "Dessert")
            recipe_lines: list = []
            for ing_str in dr.get("base", {}).get("ingredients", []):
                qty, unit, base_name = _parse_dessert_ingredient(ing_str)
                key = _normalize_ingredient(base_name)
                if not key:
                    continue
                cat = _categorize_dessert_ingredient(base_name)
                display = ing_str.split(",")[0].strip()

                if key in raw_items_map:
                    # Merge: add quantity to the existing grocery item
                    existing = raw_items_map[key]
                    if qty is not None:
                        ex_unit = (existing.get("unit") or "").lower()
                        if ex_unit == unit.lower() or not unit or not ex_unit:
                            try:
                                ex_qty = float(existing.get("amount") or 0)
                                merged = ex_qty + qty
                                existing["amount"] = str(int(merged)) if merged == int(merged) else str(round(merged, 2))
                            except (ValueError, TypeError):
                                pass
                    recipe_lines.append({**existing, "name": display})
                else:
                    unique_key = "d_" + key
                    amt_str = str(int(qty)) if qty is not None and qty == int(qty) else (str(round(qty, 2)) if qty is not None else "")
                    item = {"name": base_name, "key": unique_key, "amount": amt_str, "unit": unit or "", "category": cat, "dessert": True}
                    recipe_lines.append(item)
                    if key not in seen_dessert:
                        seen_dessert.add(key)
                        raw_items_map[unique_key] = item
                        grocery_by_category.setdefault(cat, []).append(item)
                        raw_items = raw_items + [item]
            if recipe_lines:
                grocery_by_recipe.append({"name": recipe_title, "meal_type": "dessert", "lines": recipe_lines})

    # Build dessert_calories from USDA (background) or DB/estimated calories_per_serving
    dessert_calories: dict = {}
    for di, dr in enumerate(dessert_recipes):
        dkey = f"{sk_mac}:dessert:{di}"
        title = dr.get("base", {}).get("title", "")
        if not title:
            continue
        usda_d = _macro_store.get(dkey)
        if isinstance(usda_d, list) and usda_d:
            cal = usda_d[0].get("calories", 0)
            if cal:
                dessert_calories[title] = int(round(cal * portion_scale))
                continue
        db_cal_d = _estimate_recipe_calories(dr)
        if db_cal_d:
            dessert_calories[title] = int(round(db_cal_d * portion_scale))
        # Trigger USDA computation if not yet started
        if USDA_API_KEY and dkey not in _macro_store:
            _macro_store[dkey] = "computing"
            background_tasks.add_task(_bg_compute_macros, dkey, dr, int(get_prefs(request).get("servings", 2)))

    # week_nav and view_week_label already built above (before the empty check)

    return templates.TemplateResponse(
        "plan.html",
        {
            "request": request,
            "preferences": prefs,
            "meal_labels": meal_labels,
            "accepted_clusters": accepted,
            "calendar": calendar,
            "has_meals": bool(calendar),
            "grocery_list": raw_items,
            "grocery_by_category": grocery_by_category,
            "grocery_by_recipe": grocery_by_recipe,
            "checked_items": checked,
            "week_key": view_week,
            "meal_calories": meal_calories,
            "week_nav": week_nav,
            "current_week": current_week,
            "view_week_label": view_week_label,
            "dessert_recipes": dessert_recipes,
            "dessert_calories": dessert_calories,
            "prep_checklist": prep_checklist,
            "portion_scale": portion_scale,
            "calorie_goal": calorie_goal,
        },
    )


@app.post("/planner/set")
async def planner_set(
    request: Request,
    day: str = Form(...),
    slot: str = Form(...),
    meal_name: str = Form(...),
):
    if day not in DAYS_SHORT or slot not in ("breakfast", "lunch", "dinner"):
        return HTMLResponse("", status_code=400)
    planner = request.session.get("planner", {})
    planner.setdefault(day, {})[slot] = meal_name
    request.session["planner"] = planner
    return HTMLResponse("")


@app.post("/planner/clear")
async def planner_clear(
    request: Request,
    day: str = Form(...),
    slot: str = Form(...),
):
    planner = request.session.get("planner", {})
    if day in planner and slot in planner[day]:
        del planner[day][slot]
        if not planner[day]:
            del planner[day]
    request.session["planner"] = planner
    return HTMLResponse("")


@app.post("/dessert/toggle/{recipe_id}")
async def dessert_toggle(request: Request, recipe_id: str):
    desserts: list = list(request.session.get("desserts", []))
    if recipe_id in desserts:
        desserts.remove(recipe_id)
        added = False
    else:
        desserts.append(recipe_id)
        added = True
    request.session["desserts"] = desserts
    return JSONResponse({"added": added, "id": recipe_id})


@app.get("/browse", response_class=HTMLResponse)
async def browse_recipes(request: Request):
    prefs = get_prefs(request)
    favorite_ids: list = prefs.get("favorited_recipes", [])
    selected_meal_types: list = ["breakfast", "lunch", "dinner"]

    groups: dict[str, list] = {"breakfast": [], "lunch": [], "dinner": [], "dessert": []}
    cuisine_groups: dict[str, list] = {"Italian": [], "Mexican": [], "Japanese": [], "Chinese": [], "American": [], "French": [], "Korean": [], "Mediterranean": [], "Indian": [], "Spanish": [], "Vietnamese": [], "Thai": [], "Dessert": []}
    all_entries: list = []
    recipe_data: dict = {}

    for recipe in RECIPE_DB:
        rid = recipe["_id"]
        meal_types = _RECIPE_MEAL_TYPES.get(rid, ["lunch", "dinner"])
        cuisine = _RECIPE_CUISINE.get(rid)
        use_names = [u["name"] for u in recipe.get("uses", [])]
        entry = {
            "id": rid,
            "image": recipe.get("image", ""),
            "title": recipe["base"]["title"],
            "intro": recipe.get("intro", ""),
            "search_text": " ".join([
                recipe["base"]["title"],
                recipe.get("intro", ""),
                " ".join(recipe.get("_keywords", [])),
                " ".join(use_names),
            ]).lower(),
            "uses_names": use_names,
            "meal_types": meal_types,
            "cuisine": cuisine,
        }
        recipe_data[rid] = {
            "id": rid,
            "image": entry["image"],
            "title": entry["title"],
            "intro": entry["intro"],
            "ingredients": recipe["base"].get("ingredients", []),
            "steps": recipe["base"].get("steps", []),
            "uses": [
                {
                    "name": u["name"],
                    "subtitle": u.get("subtitle", ""),
                    "image": u.get("image", ""),
                    "extras": u.get("extras", []),
                    "steps": u.get("steps", []),
                    "tip": u.get("tip"),
                }
                for u in recipe.get("uses", [])
            ],
        }
        all_entries.append(entry)
        for mt in meal_types:
            if mt in groups:
                groups[mt].append(entry)
        if cuisine and cuisine in cuisine_groups:
            cuisine_groups[cuisine].append(entry)

    # Cover image for each category card (first entry with an image)
    def _cover(entries: list) -> str:
        for e in entries:
            if e.get("image"):
                return e["image"]
        return ""

    meal_covers = {
        mt: _cover([e for e in ents if e["id"] != "miso-maple-chicken"]) or _cover(ents)
        for mt, ents in groups.items()
    }
    cuisine_covers = {c: _cover(ents) for c, ents in cuisine_groups.items()}

    # Category IDs for JS sub-view rendering
    category_ids: dict = {}
    for mt, ents in groups.items():
        category_ids[f"meal:{mt}"] = [e["id"] for e in ents]
    for c, ents in cuisine_groups.items():
        category_ids[f"cuisine:{c}"] = [e["id"] for e in ents]

    # Featured: 3 recipes matching user's meal types, prefer non-favorites, rotate weekly
    seed = int(date.today().strftime("%Y%W"))
    rng = random.Random(seed)
    eligible = [
        e for e in all_entries
        if any(mt in selected_meal_types for mt in e["meal_types"])
        and e["id"] not in favorite_ids
    ]
    if len(eligible) < 3:
        eligible = [e for e in all_entries if e["id"] not in favorite_ids]
    if len(eligible) < 3:
        eligible = list(all_entries)
    featured = rng.sample(eligible, min(6, len(eligible)))

    return templates.TemplateResponse("browse.html", {
        "request": request,
        "preferences": prefs,
        "groups": groups,
        "cuisine_groups": cuisine_groups,
        "featured": featured,
        "favorite_ids": favorite_ids,
        "meal_covers": meal_covers,
        "cuisine_covers": cuisine_covers,
        "category_ids_json": json.dumps(category_ids),
        "recipe_data_json": json.dumps(recipe_data),
    })


@app.get("/recipe/{recipe_id}")
async def get_recipe_detail(recipe_id: str):
    for recipe in RECIPE_DB:
        if recipe["_id"] == recipe_id:
            return JSONResponse({
                "id": recipe_id,
                "image": recipe.get("image", ""),
                "title": recipe["base"]["title"],
                "intro": recipe.get("intro", ""),
                "ingredients": recipe["base"].get("ingredients", []),
                "steps": recipe["base"].get("steps", []),
                "uses": [
                    {
                        "name": u["name"],
                        "subtitle": u.get("subtitle", ""),
                        "image": u.get("image", ""),
                        "extras": u.get("extras", []),
                        "steps": u.get("steps", []),
                        "tip": u.get("tip"),
                    }
                    for u in recipe.get("uses", [])
                ],
            })
    return JSONResponse({}, status_code=404)


@app.post("/favorite/{recipe_id}")
async def toggle_favorite(request: Request, recipe_id: str):
    prefs = get_prefs(request)
    favs: list = list(prefs.get("favorited_recipes", []))
    if recipe_id in favs:
        favs.remove(recipe_id)
        is_fav = False
    else:
        favs.append(recipe_id)
        is_fav = True
    prefs["favorited_recipes"] = favs
    request.session["preferences"] = prefs
    _persist_user(request)
    return JSONResponse({"favorited": is_fav, "id": recipe_id})


@app.post("/grocery/toggle/{item_key}", response_class=HTMLResponse)
async def toggle_grocery(request: Request, item_key: str, week: str = Query("")):
    checked_key = f"checked_{week}" if week else "checkedGrocery"
    checked: List[str] = request.session.get(checked_key, [])
    if item_key in checked:
        checked.remove(item_key)
    else:
        checked.append(item_key)
    request.session[checked_key] = checked
    _persist_user(request)
    is_checked = item_key in checked
    return templates.TemplateResponse(
        "partials/grocery_checkbox.html",
        {"request": request, "key": item_key, "checked": is_checked},
    )


@app.get("/grocery/substitute", response_class=HTMLResponse)
async def grocery_substitute(request: Request, ingredient: str = Query("")):
    if not ingredient:
        return HTMLResponse("")
    try:
        raw = await _gemini(
            f"Give exactly 3 common ingredient substitutions for '{ingredient}' in cooking. "
            "Format: one substitute per line, very brief (e.g. 'Greek yogurt, same amount'). "
            "No intro, no numbering, no bullets, no extra text."
        )
        subs = [s.strip() for s in raw.strip().split("\n") if s.strip()][:3]
        html_items = "".join(f"<li>{s}</li>" for s in subs)
        return HTMLResponse(
            f'<div class="sub-suggestions">'
            f'<p class="sub-suggestions-label">Swap ideas</p>'
            f'<ul class="sub-suggestions-list">{html_items}</ul>'
            f'<button class="sub-close" onclick="this.closest(\'.sub-suggestions\').remove()">✕</button>'
            f'</div>'
        )
    except Exception:
        return HTMLResponse('<div class="sub-suggestions"><p class="sub-loading">Couldn\'t load substitutions.</p></div>')


@app.get("/plan", response_class=HTMLResponse)
async def recipes_page(request: Request, background_tasks: BackgroundTasks, week: str = None):
    today = date.today()
    current_week = _week_key(today)
    plans_by_week: dict = get_plans_by_week(request)

    # Resolve view week from param or session
    if week and re.match(r"^\d{4}-W\d{2}$", week):
        view_week = week
        request.session["selectedWeek"] = week
    else:
        view_week = request.session.get("selectedWeek", current_week)

    # Save any in-progress session clusters into plans_by_week (same as /plan does)
    clusters = load_clusters(request)
    accepted_now = [c for c in clusters if c.get("accepted")]
    if accepted_now:
        prefs = get_prefs(request)
        week_days: list = prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"])
        planner: dict = request.session.get("planner", {})
        desserts = sorted(request.session.get("desserts", []))
        new_cluster_ids = sorted(c.get("_id", c.get("base", {}).get("title", "")) for c in accepted_now)
        plans_by_week[view_week] = {
            "clusters": accepted_now,
            "meal_prep_days": week_days,
            "planner": planner,
            "desserts": desserts,
        }
        save_plans_by_week(request, plans_by_week)
        sk = _store_key(request)
        last = _persisted_state.get(sk, {})
        if (last.get("week") != view_week
                or last.get("cluster_ids") != new_cluster_ids
                or last.get("planner") != planner
                or last.get("desserts") != desserts):
            _persist_user(request)
            _persisted_state[sk] = {"week": view_week, "cluster_ids": new_cluster_ids,
                                    "planner": planner, "desserts": desserts}

    # Build week nav (same pattern as grocery page)
    def _fmt_wk(wk: str) -> str:
        try:
            d = _week_dates(wk)
            return f"{d[0].strftime('%b %-d')} – {d[-1].strftime('%-d')}"
        except Exception:
            return wk

    plan_keys = set(plans_by_week.keys())
    future_keys = {_week_key(today + timedelta(weeks=i)) for i in range(0, 9)}
    all_nav = sorted(plan_keys | future_keys, reverse=True)
    view_week_label = _fmt_wk(view_week)
    week_nav = [{"key": w, "label": _fmt_wk(w), "active": w == view_week, "has_plan": w in plans_by_week} for w in all_nav]
    idx = next((i for i, w in enumerate(all_nav) if w == view_week), 0)
    prev_week_r = week_nav[idx + 1] if idx + 1 < len(week_nav) else None
    next_week_r = week_nav[idx - 1] if idx > 0 else None

    week_ctx = {
        "view_week": view_week,
        "view_week_label": view_week_label,
        "week_nav": week_nav,
        "prev_week": prev_week_r,
        "next_week": next_week_r,
        "current_week": current_week,
        "week_key": view_week,
    }

    week_plan = plans_by_week.get(view_week)
    _mt_order = {"breakfast": 0, "lunch": 1, "dinner": 2}
    accepted = sorted(
        [c for c in (week_plan.get("clusters", []) if week_plan else []) if c.get("accepted")],
        key=lambda c: _mt_order.get(c.get("mealType", ""), 99),
    )

    if not accepted:
        return templates.TemplateResponse("recipes.html", {
            "request": request,
            "recipe_clusters": None,
            "preferences": get_prefs(request),
            **week_ctx,
        })

    sk = f"{_store_key(request)}:{view_week}"
    cached = _recipe_store.get(sk)
    if not cached:
        try:
            cached = await _generate_recipes(accepted, request)
        except Exception:
            cached = []
        if cached:
            _recipe_store[sk] = cached

    prefs = get_prefs(request)
    servings: int = int(prefs.get("servings", 2))

    # Kick off USDA macro computation per cluster if not yet computed
    if cached and USDA_API_KEY:
        for ci, cluster in enumerate(cached):
            mkey = f"{sk}:{ci}"
            if mkey not in _macro_store:
                _macro_store[mkey] = "computing"
                background_tasks.add_task(_bg_compute_macros, mkey, cluster, servings)

    return templates.TemplateResponse(
        "recipes.html",
        {
            "request": request,
            "preferences": prefs,
            "recipe_clusters": cached,
            "macro_sk": sk,
            "usda_enabled": bool(USDA_API_KEY),
            **week_ctx,
        },
    )


@app.get("/api/macros/{ci}/{ui}", response_class=HTMLResponse)
async def macros_fragment(request: Request, ci: int, ui: int, sk: str = ""):
    """HTMX polling endpoint — returns one use's macro pills, or empty to keep polling."""
    if not sk:
        return HTMLResponse("")
    mkey = f"{sk}:{ci}"
    result = _macro_store.get(mkey)
    if result == "computing" or result is None:
        return HTMLResponse("")  # still working — poll again
    if not result or ui >= len(result):
        return HTMLResponse(f'<div id="macros-{ci}-{ui}"></div>')  # nothing to show, stop polling
    m = result[ui]
    return HTMLResponse(
        f'<div id="macros-{ci}-{ui}" class="macro-pills">'
        f'<span class="macro-pill macro-cal">{m["calories"]} kcal</span>'
        f'<span class="macro-pill macro-p">{m["protein_g"]}g protein</span>'
        f'<span class="macro-pill macro-c">{m["carbs_g"]}g carbs</span>'
        f'<span class="macro-pill macro-f">{m["fat_g"]}g fat</span>'
        f'</div>'
    )


@app.get("/api/macros-panel/{ci}", response_class=HTMLResponse)
async def macros_panel(request: Request, background_tasks: BackgroundTasks, ci: int, sk: str = ""):
    """HTMX endpoint — returns full macro table for all uses of a cluster."""
    if not sk:
        return HTMLResponse("")
    if not USDA_API_KEY:
        return HTMLResponse('<p style="color:var(--text-3);font-size:.8rem;padding:10px 0;">Add <code>USDA_API_KEY</code> to your .env file to enable macro tracking.</p>')
    mkey = f"{sk}:{ci}"
    result = _macro_store.get(mkey)
    if result is None:
        # Not yet started or previously failed — retry if we have the cluster data
        cached = _recipe_store.get(sk, [])
        if cached and ci < len(cached):
            prefs = get_prefs(request)
            servings = int(prefs.get("servings", 2))
            _macro_store[mkey] = "computing"
            background_tasks.add_task(_bg_compute_macros, mkey, cached[ci], servings)
            result = "computing"
    if result == "computing":
        return HTMLResponse(
            f'<div hx-get="/api/macros-panel/{ci}?sk={sk}" hx-trigger="every 4s" hx-swap="outerHTML"'
            f' style="color:var(--text-3);font-size:.8rem;padding:10px 0;">Computing macros…</div>'
        )
    if not result or not isinstance(result, list):
        return HTMLResponse('<p style="color:var(--text-3);font-size:.8rem;padding:10px 0;">Macros unavailable for this recipe.</p>')
    cached = _recipe_store.get(sk, [])
    cluster = cached[ci] if cached and ci < len(cached) else {}
    uses = cluster.get("uses", [])
    rows = ""
    for ui, m in enumerate(result):
        name = uses[ui].get("name", f"Variation {ui + 1}") if ui < len(uses) else f"Variation {ui + 1}"
        rows += (
            f"<tr>"
            f"<td class='mp-name'>{name}</td>"
            f"<td class='mp-num'>{m.get('calories', '—')}</td>"
            f"<td class='mp-num'>{m.get('protein_g', '—')}g</td>"
            f"<td class='mp-num'>{m.get('fat_g', '—')}g</td>"
            f"<td class='mp-num'>{m.get('carbs_g', '—')}g</td>"
            f"</tr>"
        )
    html = (
        "<table class='macros-table'>"
        "<thead><tr>"
        "<th>Variation</th><th>Cal</th><th>Protein</th><th>Fat</th><th>Carbs</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )
    return HTMLResponse(html)


@app.post("/recipes/regenerate")
async def regenerate_recipes(request: Request):
    sk = _store_key(request)
    for k in list(_recipe_store.keys()):
        if k.startswith(sk):
            del _recipe_store[k]
    return RedirectResponse("/plan", status_code=303)


@app.post("/day/complete/{date_str}", response_class=HTMLResponse)
async def toggle_day_complete(request: Request, date_str: str):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return HTMLResponse("", status_code=400)
    completions = dict(request.session.get("dayCompletions", {}))
    completions[date_str] = not completions.get(date_str, False)
    request.session["dayCompletions"] = completions
    is_complete = completions[date_str]
    cls = "complete" if is_complete else "planned"
    label = "Complete" if is_complete else "Planned"
    return HTMLResponse(
        f'<button class="day-status-btn {cls}" '
        f'onclick="fetch(\'/day/complete/{date_str}\',{{method:\'POST\'}}).then(r=>r.text()).then(h=>{{this.outerHTML=h}})">'
        f"{label}</button>"
    )


@app.post("/select-week")
async def select_week(
    request: Request,
    week: str = Form(...),
    cal_year: int = Form(None),
    cal_month: int = Form(None),
    next_url: str = Form("/landing"),
):
    if re.match(r"^\d{4}-W\d{2}$", week):
        request.session["selectedWeek"] = week
    if cal_year:
        request.session["calYear"] = cal_year
    if cal_month:
        request.session["calMonth"] = cal_month
    if next_url not in ("/landing", "/landing#main-page", "/dashboard"):
        next_url = "/landing"
    return RedirectResponse(next_url, status_code=303)


@app.post("/calendar-nav")
async def calendar_nav(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
    next_url: str = Form("/landing"),
):
    request.session["calYear"] = year
    request.session["calMonth"] = month
    if next_url not in ("/landing", "/landing#main-page", "/dashboard"):
        next_url = "/landing"
    return RedirectResponse(next_url, status_code=303)


@app.get("/new-plan")
async def new_plan(request: Request):
    """Clear clusters but keep preferences - takes user back to choose mode."""
    clear_clusters(request)
    _recipe_store.pop(_store_key(request), None)
    request.session.pop("existingIngredients", None)
    request.session.pop("cuisinePreferences", None)
    request.session.pop("checkedGrocery", None)
    request.session.pop("mode", None)
    request.session.pop("weekDays", None)
    request.session.pop("planner", None)
    return RedirectResponse("/mode")


@app.post("/plan-week")
async def plan_week_route(request: Request, week: str = Form(...)):
    """Start a completely fresh plan for a specific week (wipes all plan state)."""
    if re.match(r"^\d{4}-W\d{2}$", week):
        request.session["selectedWeek"] = week
    clear_clusters(request)
    _recipe_store.pop(_store_key(request), None)
    for key in ["existingIngredients", "cuisinePreferences", "checkedGrocery",
                "mode", "weekDays", "planner", "desserts", "preferences"]:
        request.session.pop(key, None)
    return RedirectResponse("/setup", status_code=303)


@app.post("/plan/copy")
async def copy_plan(request: Request, from_week: str = Form(...), to_week: str = Form(...)):
    """Copy a full plan from one week to another."""
    if not (re.match(r"^\d{4}-W\d{2}$", from_week) and re.match(r"^\d{4}-W\d{2}$", to_week)):
        return RedirectResponse("/plan", status_code=303)
    plans = get_plans_by_week(request)
    src = plans.get(from_week)
    if src and from_week != to_week:
        import copy as _copy
        plans[to_week] = _copy.deepcopy(src)
        save_plans_by_week(request, plans)
        request.session["selectedWeek"] = to_week
    return RedirectResponse(f"/plan?week={to_week}", status_code=303)


@app.get("/reset")
async def reset(request: Request):
    clear_clusters(request)
    request.session.clear()
    return RedirectResponse("/")
