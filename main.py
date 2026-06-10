from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import anthropic
import json
import os
import re
import uuid
from typing import List
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "mealprepped-dev-secret-change-in-prod"),
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def get_anthropic_client():
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    return anthropic.Anthropic(api_key=key)


# ── Routes ──────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/setup")
async def setup(
    request: Request,
    meal_types: List[str] = Form(default=[]),
    servings: int = Form(2),
    dietary_needs: str = Form(""),
):
    if not meal_types:
        meal_types = ["dinner"]
    request.session["preferences"] = {
        "mealTypes": meal_types,
        "servings": servings,
        "dietaryNeeds": dietary_needs.strip(),
    }
    return RedirectResponse("/mode", status_code=303)


@app.get("/mode", response_class=HTMLResponse)
async def mode_page(request: Request):
    if not request.session.get("preferences"):
        return RedirectResponse("/")
    prefs = request.session["preferences"]
    return templates.TemplateResponse("mode.html", {"request": request, "preferences": prefs})


@app.post("/mode")
async def set_mode(request: Request, mode: str = Form(...)):
    request.session["mode"] = mode
    request.session["clusters"] = []
    request.session["checkedGrocery"] = []
    if mode == "existing":
        return RedirectResponse("/ingredients", status_code=303)
    return RedirectResponse("/suggest", status_code=303)


@app.get("/ingredients", response_class=HTMLResponse)
async def ingredients_page(request: Request):
    if not request.session.get("preferences"):
        return RedirectResponse("/")
    return templates.TemplateResponse("ingredients.html", {"request": request})


@app.post("/ingredients")
async def save_ingredients(request: Request, ingredients: str = Form("")):
    items = [i.strip() for i in ingredients.split(",") if i.strip()]
    request.session["existingIngredients"] = items
    return RedirectResponse("/suggest", status_code=303)


@app.get("/suggest", response_class=HTMLResponse)
async def suggest_page(request: Request):
    if not request.session.get("preferences"):
        return RedirectResponse("/")
    clusters = request.session.get("clusters", [])
    prefs = request.session["preferences"]
    mode = request.session.get("mode", "suggestions")
    existing = request.session.get("existingIngredients", [])
    return templates.TemplateResponse(
        "suggest.html",
        {
            "request": request,
            "preferences": prefs,
            "clusters": clusters,
            "has_clusters": bool(clusters),
            "mode": mode,
            "existing_ingredients": existing,
        },
    )


@app.post("/generate", response_class=HTMLResponse)
async def generate_suggestions(request: Request):
    prefs = request.session.get("preferences")
    if not prefs:
        return HTMLResponse("<p class='error-msg'>Session expired. <a href='/'>Start over</a></p>")

    meal_types: List[str] = prefs.get("mealTypes", ["dinner"])
    servings: int = prefs.get("servings", 2)
    dietary_needs: str = prefs.get("dietaryNeeds", "")
    existing: List[str] = request.session.get("existingIngredients", [])

    existing_note = ""
    if existing:
        existing_note = (
            f"\nThe user already has these ingredients: {', '.join(existing)}. "
            "Set \"userHas\": true for any of these in every cluster that uses them."
        )

    meal_sections = "\n    ".join(f'"{mt}": [<2 cluster objects>]' for mt in meal_types)

    system_prompt = (
        "You are a meal planning assistant. Generate efficient, ingredient-sharing meal "
        "clusters for a full week. Return ONLY valid JSON — no markdown fences, no explanation."
    )

    user_prompt = f"""Create weekly meal plan suggestions.

User:
- Wants: {', '.join(meal_types)}
- Servings: {servings} people
- Dietary restrictions: {dietary_needs or 'none'}
{existing_note}

Rules:
- Suggest exactly 2 clusters per requested meal type
- Each cluster = 2-3 meals sharing the same core ingredients (e.g. same protein + veg used as wrap, bowl, salad)
- Together, the clusters should cover a full 7-day week
- Be practical and fresh — real recipes people love

Return this exact JSON shape:
{{
  "suggestions": {{
    {meal_sections}
  }}
}}

Each cluster object:
{{
  "id": "short_unique_id",
  "clusterName": "Catchy name",
  "tagline": "One-line ingredient concept",
  "meals": [
    {{"name": "Meal Name", "description": "1-2 sentence description", "suggestedDay": "e.g. Mon & Wed"}}
  ],
  "ingredients": [
    {{"name": "ingredient name", "amount": "2", "unit": "cups", "userHas": false}}
  ],
  "cookTime": "30 min",
  "difficulty": "Easy"
}}"""

    try:
        client = get_anthropic_client()
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = message.content[0].text
        json_match = re.search(r"\{[\s\S]*\}", raw)
        if not json_match:
            raise ValueError("No JSON object found in response")

        data = json.loads(json_match.group(0))
        suggestions = data.get("suggestions", {})

        clusters = []
        for meal_type, type_clusters in suggestions.items():
            for cluster in type_clusters:
                cluster["mealType"] = meal_type
                cluster["accepted"] = False
                if not cluster.get("id"):
                    cluster["id"] = str(uuid.uuid4())[:8]
                clusters.append(cluster)

        request.session["clusters"] = clusters

        return templates.TemplateResponse(
            "partials/suggestions_grid.html",
            {"request": request, "clusters": clusters, "meal_types": meal_types},
        )

    except Exception as exc:
        return templates.TemplateResponse(
            "partials/generate_error.html",
            {"request": request, "error": str(exc)},
        )


@app.post("/toggle/{cluster_id}", response_class=HTMLResponse)
async def toggle_cluster(request: Request, cluster_id: str):
    clusters = request.session.get("clusters", [])
    for cluster in clusters:
        if cluster["id"] == cluster_id:
            cluster["accepted"] = not cluster.get("accepted", False)
            break
    request.session["clusters"] = clusters

    target = next((c for c in clusters if c["id"] == cluster_id), None)
    if not target:
        return HTMLResponse("<p>Not found</p>")

    return templates.TemplateResponse(
        "partials/cluster_card.html",
        {"request": request, "cluster": target},
    )


@app.get("/plan", response_class=HTMLResponse)
async def plan_page(request: Request):
    clusters = request.session.get("clusters", [])
    accepted = [c for c in clusters if c.get("accepted")]
    if not accepted:
        return RedirectResponse("/suggest")

    checked: List[str] = request.session.get("checkedGrocery", [])

    grocery_map: dict = {}
    for cluster in accepted:
        for ing in cluster.get("ingredients", []):
            if not ing.get("userHas", False):
                key = ing["name"].lower().strip()
                if key not in grocery_map:
                    grocery_map[key] = {**ing, "key": key}

    grocery_list = list(grocery_map.values())

    return templates.TemplateResponse(
        "plan.html",
        {
            "request": request,
            "accepted_clusters": accepted,
            "grocery_list": grocery_list,
            "checked_items": checked,
        },
    )


@app.post("/grocery/toggle/{item_key}", response_class=HTMLResponse)
async def toggle_grocery(request: Request, item_key: str):
    checked: List[str] = request.session.get("checkedGrocery", [])
    if item_key in checked:
        checked.remove(item_key)
    else:
        checked.append(item_key)
    request.session["checkedGrocery"] = checked
    is_checked = item_key in checked
    return templates.TemplateResponse(
        "partials/grocery_checkbox.html",
        {"request": request, "key": item_key, "checked": is_checked},
    )


@app.get("/reset")
async def reset(request: Request):
    request.session.clear()
    return RedirectResponse("/")
