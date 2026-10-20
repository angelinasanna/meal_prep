"""USDA FoodData Central lookups and ingredient macro math."""

from dotenv import load_dotenv
import asyncio
import httpx
import os
import re
from state import _macro_store

load_dotenv()


_usda_cache: dict = {}       # normalized food name -> per-100g nutrient dict (persistent in-process cache)

# ── USDA FoodData Central ─────────────────────────────────────
USDA_API_KEY: str = os.getenv("USDA_API_KEY", "")

# Unicode fraction characters → float
_UNICODE_FRACS: dict = {
    "½": 0.5, "⅓": 1/3, "⅔": 2/3, "¼": 0.25, "¾": 0.75,
    "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}

# Weight units → grams
_WEIGHT_G: dict = {
    "g": 1, "gram": 1, "grams": 1,
    "kg": 1000, "kilogram": 1000, "kilograms": 1000,
    "oz": 28.35, "ounce": 28.35, "ounces": 28.35,
    "lb": 453.6, "lbs": 453.6, "pound": 453.6, "pounds": 453.6,
}

# Count/display units — recognized for grocery display but no gram conversion
_DISPLAY_UNITS: set = {
    "clove", "cloves", "bunch", "bunches", "stalk", "stalks",
    "head", "heads", "sprig", "sprigs", "slice", "slices",
    "can", "cans", "jar", "jars", "package", "pkg", "packages",
    "piece", "pieces", "fillet", "fillets", "sheet", "sheets",
    "strip", "strips", "link", "links", "block", "blocks",
}

# Volume units → ml (treated as grams at water density; close enough for produce/sauces)
_VOLUME_ML: dict = {
    "cup": 240, "cups": 240,
    "tbsp": 15, "tablespoon": 15, "tablespoons": 15,
    "tsp": 5, "teaspoon": 5, "teaspoons": 5,
    "ml": 1, "milliliter": 1, "milliliters": 1,
    "l": 1000, "liter": 1000, "liters": 1000,
    "fl oz": 29.57,
}

# Words to strip from ingredient strings before food-name lookup
_STRIP_WORDS: set = {
    "shredded", "diced", "chopped", "minced", "sliced", "cooked", "fresh",
    "dried", "ground", "grated", "peeled", "boneless", "skinless", "frozen",
    "canned", "drained", "rinsed", "raw", "large", "medium", "small", "extra",
    "fine", "coarse", "organic", "halved", "quartered", "trimmed", "pitted",
    "deveined", "butterflied", "cubed", "crumbled", "softened", "divided",
    "plus", "more", "to", "taste", "or", "and", "of", "about", "approximately",
    "packed", "heaping", "level", "hot", "cold", "warm", "room", "temperature",
    "lightly", "well", "roughly", "thinly", "thickly", "finely",
    "cut", "into", "strips", "florets", "pieces", "cubes", "rings", "wedges",
    "lengthwise", "crosswise", "separated", "broken", "torn", "flaked",
    "very", "as", "needed", "use", "reserved", "remaining",
    "ripe", "firm", "lean", "whole",
}

# Approximate gram weights for common unit-count ingredients (1 unit → grams)
_UNIT_GRAM_DEFAULTS: dict = {
    "avocado": 150, "tomato": 120, "egg": 50, "lemon": 60, "lime": 60,
    "onion": 110, "garlic": 5, "potato": 150, "sweet potato": 130,
    "carrot": 80, "zucchini": 200, "pepper": 120, "bell pepper": 120,
    "chicken breast": 175, "chicken thigh": 130, "salmon fillet": 150,
    "apple": 180, "banana": 120, "orange": 150,
    "can": 400, "tin": 400,
}

# USDA nutrient IDs we care about
_USDA_NIDS: dict = {
    1008: "calories",  # Energy (kcal)
    1003: "protein_g", # Protein
    1005: "carbs_g",   # Carbohydrate, by difference
    1004: "fat_g",     # Total lipid (fat)
}


def _parse_qty(s: str) -> tuple:
    """Extract leading numeric quantity from string. Returns (float, remainder)."""
    for ch, val in _UNICODE_FRACS.items():
        s = s.replace(ch, f" {val} ")
    s = re.sub(r"\s+", " ", s).strip()
    # Mixed number: "1 1/2"
    m = re.match(r"^(\d+\.?\d*)\s+(\d+)\s*/\s*(\d+)", s)
    if m:
        return float(m[1]) + float(m[2]) / float(m[3]), s[m.end():].strip()
    # Simple fraction: "1/2"
    m = re.match(r"^(\d+)\s*/\s*(\d+)", s)
    if m:
        return float(m[1]) / float(m[2]), s[m.end():].strip()
    # Decimal or integer (optionally followed by a range like "-4" which we ignore)
    m = re.match(r"^(\d+\.?\d*)(?:\s*[-–]\s*\d+\.?\d*)?", s)
    if m:
        return float(m[1]), s[m.end():].strip()
    return 1.0, s


def _parse_ingredient(raw: str) -> dict:
    """Parse ingredient string → {food_name, qty_g, qty, unit}."""
    raw = raw.strip().lstrip("+-•* ").strip()
    # Remove parenthetical notes e.g. "(optional)"
    raw = re.sub(r"\(.*?\)", "", raw).strip()
    qty, rest = _parse_qty(raw)
    rest = rest.strip()
    unit = None
    qty_g = None
    # Try longest unit match first (weight + volume → gives qty_g for USDA)
    all_units = {**_WEIGHT_G, **_VOLUME_ML}
    for u in sorted(all_units, key=len, reverse=True):
        pat = re.compile(r"^" + re.escape(u) + r"(?:\s|$|\.)", re.IGNORECASE)
        if pat.match(rest):
            rest = rest[len(u):].lstrip(". ").strip()
            unit = u
            qty_g = qty * all_units[u.lower()]
            break
    # If no weight/volume unit found, try display-only count units
    if unit is None:
        for u in sorted(_DISPLAY_UNITS, key=len, reverse=True):
            pat = re.compile(r"^" + re.escape(u) + r"(?:\s|$|\.)", re.IGNORECASE)
            if pat.match(rest):
                rest = rest[len(u):].lstrip(". ").strip()
                unit = u
                break
    # Strip "of" connector
    rest = re.sub(r"^of\s+", "", rest, flags=re.IGNORECASE)
    # Build food name: stop at first comma (removes prep text like "cut into strips"),
    # then keep non-stopword alphabetic words, max 4
    rest_for_name = rest.split(",")[0].strip()
    words = rest_for_name.split()
    food_words = [w for w in words if w and w.lower() not in _STRIP_WORDS and re.match(r"[a-zA-Z]", w)]
    food_name = " ".join(food_words[:4]).strip() or rest_for_name[:40]
    return {"food_name": food_name, "qty_g": qty_g, "qty": qty, "unit": unit}


async def _usda_lookup(food_name: str) -> dict:
    """Return per-100g {calories, protein_g, carbs_g, fat_g} from USDA FDC, or {} on failure."""
    key = food_name.lower().strip()
    if key in _usda_cache:
        return _usda_cache[key]
    if not USDA_API_KEY:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.nal.usda.gov/fdc/v1/foods/search",
                params={
                    "query": food_name,
                    "api_key": USDA_API_KEY,
                    "dataType": "SR Legacy,Foundation,FNDDS",
                    "pageSize": 1,
                },
            )
        if resp.status_code != 200:
            _usda_cache[key] = {}
            return {}
        foods = resp.json().get("foods", [])
        if not foods:
            _usda_cache[key] = {}
            return {}
        result: dict = {}
        for n in foods[0].get("foodNutrients", []):
            nid = n.get("nutrientId")
            if nid in _USDA_NIDS:
                result[_USDA_NIDS[nid]] = float(n.get("value", 0))
        _usda_cache[key] = result
        return result
    except Exception:
        return {}


async def _macros_for_ingredients(ingredients: list, servings: int) -> dict:
    """Sum USDA macros for a list of raw ingredient strings, scaled to per-serving."""
    parsed = [_parse_ingredient(i) for i in ingredients if isinstance(i, str)]
    lookups = await asyncio.gather(*[_usda_lookup(p["food_name"]) for p in parsed])
    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    for p, per_100g in zip(parsed, lookups):
        if not per_100g:
            continue
        qty_g = p["qty_g"]
        if qty_g is None and p["qty"] is not None:
            # Count-based ingredient (e.g. "1 avocado") — look up a known weight or use 100g default
            food_lower = p["food_name"].lower()
            default_g = next((v for k, v in _UNIT_GRAM_DEFAULTS.items() if k in food_lower), 100)
            qty_g = p["qty"] * default_g
        if not qty_g:
            continue
        scale = qty_g / 100.0
        for k in totals:
            totals[k] += per_100g.get(k, 0.0) * scale
    sv = max(servings, 1)
    return {k: round(v / sv, 1) for k, v in totals.items()}


async def _compute_recipe_macros(recipe: dict, servings: int) -> list:
    """Return list of per-use per-serving macro dicts for a recipe cluster."""
    uses = recipe.get("uses", [])
    if not uses:
        return []
    base_ings = [i for i in recipe.get("base", {}).get("ingredients", [])
                 if not (isinstance(i, str) and i.startswith("[") and i.endswith("]"))]
    # Base macros split across all uses
    base_macros = await _macros_for_ingredients(base_ings, servings * len(uses))
    use_macros = []
    for use in uses:
        extras = use.get("extras", [])
        extras_macros = await _macros_for_ingredients(extras, servings) if extras else {}
        use_macros.append({
            "calories": round(base_macros.get("calories", 0) + extras_macros.get("calories", 0)),
            "protein_g": round(base_macros.get("protein_g", 0) + extras_macros.get("protein_g", 0), 1),
            "carbs_g": round(base_macros.get("carbs_g", 0) + extras_macros.get("carbs_g", 0), 1),
            "fat_g": round(base_macros.get("fat_g", 0) + extras_macros.get("fat_g", 0), 1),
        })
    return use_macros


async def _bg_compute_macros(mkey: str, recipe: dict, servings: int):
    """Background task: compute macros for one recipe and store in _macro_store."""
    try:
        result = await _compute_recipe_macros(recipe, servings)
        _macro_store[mkey] = result
    except Exception:
        _macro_store[mkey] = None  # signal failure so polling stops
