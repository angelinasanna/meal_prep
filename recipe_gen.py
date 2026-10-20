"""Recipe card generation: database match first, then the model, then a fallback."""

from fastapi import Request
import asyncio
import json
import re
from llm import _gemini
from recipe_content import _find_db_recipe
from state import get_prefs


def _db_recipe_for_cluster(cluster_summary: dict, used_db_ids: set) -> dict | None:
    """Return a DB recipe for this cluster if one matches and hasn't been used yet."""
    db_match = _find_db_recipe(cluster_summary)
    if db_match and db_match["_id"] not in used_db_ids:
        used_db_ids.add(db_match["_id"])
        recipe = {k: v for k, v in db_match.items() if not k.startswith("_")}
        recipe["id"] = cluster_summary["id"]
        meal_names = cluster_summary.get("meals", [])
        if meal_names and recipe.get("uses"):
            uses = list(recipe["uses"])
            while len(uses) < len(meal_names):
                uses.append(dict(uses[-1]))
            uses = uses[:len(meal_names)]
            for i, name in enumerate(meal_names):
                uses[i] = {**uses[i], "name": name}
            recipe["uses"] = uses
        return recipe
    return None


async def _ai_recipe(cluster_summary: dict, servings: int, dietary_note: str, other_bases: list[str]) -> dict | None:
    """Generate a recipe via AI. other_bases lists base titles already used. Avoid repeating them."""
    meal_names: list = cluster_summary.get("meals", [])

    uses_instruction = ""
    if meal_names:
        names_list = "\n".join(f'  {i+1}. "{n}"' for i, n in enumerate(meal_names))
        uses_instruction = f"""
The "uses" array must have exactly {len(meal_names)} entries, one per meal.
Use these exact strings as each "name" field, in this order:
{names_list}"""

    avoid_note = ""
    if other_bases:
        avoid_note = f"\nThe base prep title must be DIFFERENT from these already-used bases: {', '.join(other_bases)}. Use a distinct cooking method or protein."

    prompt = f"""Write a meal prep recipe cluster. Warm, practical tone. One base cooked once, used multiple ways. Servings: {servings}.{dietary_note}{avoid_note}

Cluster:
{json.dumps(cluster_summary, indent=2)}
{uses_instruction}

INGREDIENT COMPLETENESS (critical):
- Include EVERY ingredient actually needed: salt, sugar, oil, butter, baking powder, baking soda, vinegar, soy sauce, spices, seasonings, herbs, flour, eggs, dairy — nothing omitted.
- If a real recipe for this dish requires a leavening agent, sweetener, fat, acid, or seasoning, it must appear in the list.
- Format each as "amount unit ingredient", e.g. "1 tsp baking powder", "2 tbsp sugar", "0.5 tsp salt".
- A cook should be able to make this from the list alone without looking anything up.

CRITICAL JSON RULES:
- Straight double quotes only. No curly/smart quotes.
- No contractions: "do not" not "don't", "it is" not "it's".
- No double-quote characters inside string values.
- No trailing commas.

Return exactly this JSON (no markdown):
{{
  "id": "{cluster_summary['id']}",
  "intro": "One warm sentence about why this base works for meal prep.",
  "base": {{
    "title": "Base Prep Title",
    "ingredients": ["amount unit ingredient", "..."],
    "steps": ["Step 1...", "..."]
  }},
  "uses": [
    {{
      "name": "Exact meal name from the list above",
      "subtitle": "One-line description",
      "extras": ["extra ingredient"],
      "steps": ["Step 1...", "..."],
      "tip": "Optional tip or null"
    }}
  ]
}}"""

    raw = await _gemini(prompt)
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        return None
    json_str = re.sub(r",\s*([}\]])", r"\1", json_match.group(0))
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


def _fallback_recipe_from_cluster(cluster_summary: dict) -> dict:
    """Build a minimal recipe card from cluster data when AI generation fails."""
    meal_names = cluster_summary.get("meals", [])
    return {
        "id": cluster_summary.get("id"),
        "intro": cluster_summary.get("tagline", ""),
        "base": {
            "title": cluster_summary.get("name", "Meal Prep"),
            "ingredients": cluster_summary.get("ingredients", []),
            "steps": [
                "Prep and measure all ingredients.",
                "Cook using your preferred method.",
                "Divide into portions and store in airtight containers for up to 4 days.",
            ],
        },
        "uses": [
            {
                "name": name,
                "extras": [],
                "steps": [f"Serve with the base prep."],
                "tip": None,
            }
            for name in meal_names
        ],
    }


async def _generate_recipes(accepted: list, request: Request) -> list:
    prefs = get_prefs(request)
    servings = prefs.get("servings", 2)
    dietary = prefs.get("dietaryNeeds", "")
    dietary_note = f" Dietary needs: {dietary}." if dietary else ""

    clusters_summary = []
    for c in accepted:
        clusters_summary.append({
            "id": c.get("id"),
            "name": c.get("clusterName", ""),
            "tagline": c.get("tagline", ""),
            "meals": [m.get("name") for m in c.get("meals", [])],
            "ingredients": [
                f"{ing.get('amount','')} {ing.get('unit','')} {ing.get('name','')}".strip()
                for ing in c.get("ingredients", [])
            ],
        })

    # Phase 1: sequential DB matching. Each DB entry used at most once
    used_db_ids: set = set()
    plan: list = []  # list of ("db", recipe) or ("ai", cluster_summary)
    for cs in clusters_summary:
        db_recipe = _db_recipe_for_cluster(cs, used_db_ids)
        if db_recipe:
            plan.append(("db", db_recipe))
        else:
            plan.append(("ai", cs))

    # Phase 2: collect base titles already assigned from DB, then run AI in parallel
    db_bases = [entry["base"]["title"] for kind, entry in plan if kind == "db"]
    ai_items = [(i, cs) for i, (kind, cs) in enumerate(plan) if kind == "ai"]

    ai_results = await asyncio.gather(*[
        _ai_recipe(cs, servings, dietary_note, db_bases)
        for _, cs in ai_items
    ], return_exceptions=True)
    ai_results = [r if not isinstance(r, BaseException) else None for r in ai_results]

    # Merge back in original order; fall back to cluster data if AI fails
    ai_iter = iter(ai_results)
    final = []
    for kind, data in plan:
        if kind == "db":
            final.append(data)
        else:
            r = next(ai_iter)
            final.append(r if r else _fallback_recipe_from_cluster(data))

    return final
