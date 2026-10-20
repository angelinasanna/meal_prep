"""Cuisine/season constants, recipe name helpers and ingredient normalisation."""

import re
from recipe_data import RECIPE_DB


# ── Recipe variation name helpers ─────────────────────────────
# Words too generic to count as "distinctive" when deciding whether to append
# a recipe-title suffix to a variation name.
_VAR_STOP: set = {
    "easy", "simple", "quick", "classic", "homemade", "fresh", "light", "healthy",
    "crispy", "crunchy", "baked", "roasted", "grilled", "sauteed", "fried", "spiced",
    "smoked", "stuffed", "glazed", "braised", "marinated", "pickled", "whipped", "poached",
    "pan", "one", "pot", "sheet", "slow", "instant", "air",
    "with", "and", "the", "of", "in", "a", "an", "on", "for", "to", "by",
    "style", "inspired",
    "salad", "bowl", "plate", "dish", "meal", "soup", "stew", "wrap",
    "sandwich", "toast", "taco", "burger", "sauce", "dressing",
    "vegetable", "veggie",
}


def _content_words(segment: str) -> list:
    """Non-stop, ≥3-char words from a title segment, split on spaces/hyphens/slashes."""
    return [w for w in re.split(r"[\s\-/&,]+", segment)
            if w and re.match(r"[a-zA-Z]{3,}", w) and w.lower() not in _VAR_STOP]


def _recipe_name_context(title: str) -> dict:
    """Analyse a recipe title and return the prefix to add to variation names.

    Two formats:
      • "with" recipe  (e.g. "Turmeric Salmon with Parsley Yogurt Sauce"):
          with_key = "Turmeric Salmon"  → "{with_key} and {variation}"
          ("Salmon with Pistachio Pesto") → with_key = "Pistachio Salmon" (1 from each side)
      • plain recipe   (e.g. "Lemon-Marinated Bean and Vegetable Salad"):
          full_ctx = "Lemon Bean"       → "{full_ctx} {variation}"
    """
    parts = re.split(r"\s+with\s+", title, flags=re.IGNORECASE)
    if len(parts) >= 2:
        before = _content_words(parts[0])
        after  = _content_words(" ".join(parts[1:]))
        if len(before) >= 2:
            with_key = " ".join(before[:2])          # "Turmeric Salmon"
        elif before and after:
            with_key = f"{after[0]} {before[0]}"     # "Pistachio Salmon"
        else:
            with_key = " ".join((before or after)[:2])
        return {"with_key": with_key, "full_ctx": ""}
    words = _content_words(title)
    full_ctx = " ".join(words[:2]) if words else ""  # "Lemon Bean"
    return {"with_key": "", "full_ctx": full_ctx}


# ── Cuisine / season constants ───────────────────────────────────────────────
CUISINES = [
    "Italian", "Mexican", "Japanese", "Chinese",
    "Indian", "Thai", "Korean", "French",
    "Mediterranean", "American", "Spanish", "Vietnamese",
]

_SEASON_MAP = {
    12: "Winter", 1: "Winter", 2: "Winter",
    3: "Spring",  4: "Spring",  5: "Spring",
    6: "Summer",  7: "Summer",  8: "Summer",
    9: "Fall",   10: "Fall",   11: "Fall",
}

SEASONS = ["Spring", "Summer", "Fall", "Winter"]

_SEASON_GUIDANCE = {
    "Spring": "seasonal spring produce (asparagus, peas, artichokes, radishes, spring greens, leeks). Light, fresh preparations.",
    "Summer": "seasonal summer produce (tomatoes, zucchini, corn, peppers, basil, stone fruits, cucumbers, berries). Fresh, lighter meals. Grilling works well.",
    "Fall":   "seasonal fall produce (butternut squash, sweet potato, apples, Brussels sprouts, mushrooms, root vegetables). Warming, hearty dishes.",
    "Winter": "seasonal winter produce (citrus, kale, root vegetables, cabbage, pears, dried legumes). Comforting soups, stews, and braises.",
}


def _find_db_recipe(cluster_summary: dict) -> dict | None:
    """Return a matching RECIPE_DB entry if the cluster's protein/style matches."""
    cluster_name = cluster_summary.get("name", "").lower()
    search_text = " ".join([
        cluster_name,
        cluster_summary.get("tagline", ""),
        " ".join(cluster_summary.get("ingredients", [])),
        " ".join(str(m) for m in cluster_summary.get("meals", [])),
    ]).lower()

    _STOP = {"and", "the", "with", "for", "from", "over", "into", "that", "this", "are", "your"}

    for recipe in RECIPE_DB:
        # Primary: ALL significant words from recipe title must appear in cluster name
        title_words = [
            w.strip("(),").lower() for w in recipe["base"]["title"].split()
            if len(w) > 3 and w.lower() not in _STOP
        ]
        if title_words and all(w in cluster_name for w in title_words):
            return recipe
        # Fallback: ALL keywords must be present (original strict behaviour)
        keywords = recipe.get("_keywords", [])
        if keywords and all(kw.lower() in search_text for kw in keywords):
            return recipe
    return None


# ── Vegetarian inference ─────────────────────────────────────────────────────
_MEAT_KEYWORDS = {
    'chicken', 'beef', 'pork', 'lamb', 'turkey', 'salmon', 'tuna', 'shrimp',
    'bacon', 'sausage', 'ham', 'steak', 'meat', 'fish', 'seafood', 'crab',
    'lobster', 'duck', 'veal', 'bison', 'venison', 'anchovy', 'sardine',
    'prosciutto', 'pepperoni', 'chorizo', 'salami', 'mince', 'tilapia',
    'cod', 'halibut', 'mahi', 'scallop', 'mussel', 'clam', 'oyster',
}

_INGREDIENT_ALIASES: dict[str, str] = {
    # Herb "fresh X" → X (removes duplicate fresh/non-fresh entries)
    "fresh parsley": "parsley", "flat-leaf parsley": "parsley", "curly parsley": "parsley",
    "fresh cilantro": "cilantro", "fresh basil": "basil", "fresh tarragon": "tarragon",
    "fresh thyme": "thyme", "fresh rosemary": "rosemary", "fresh mint": "mint",
    "fresh dill": "dill", "fresh chives": "chives", "fresh oregano": "oregano",
    "fresh sage": "sage", "fresh ginger": "ginger",
    # Garlic singular/plural
    "garlic cloves": "garlic", "garlic clove": "garlic",
    # Singular → plural for common items
    "poblano pepper": "poblano peppers",
    "scallion": "scallions", "green onion": "scallions", "green onions": "scallions",
    "limes": "lime", "lemons": "lemon",
    "shallot": "shallots",
}


def _normalize_ingredient(name: str) -> str:
    n = name.lower().strip()
    if n in _INGREDIENT_ALIASES:
        return _INGREDIENT_ALIASES[n]
    # Strip standalone "fresh " prefix for any herb not in the alias table
    if n.startswith("fresh "):
        return n[6:]
    return n


def _infer_vegetarian(ingredients: list) -> bool:
    """True if ingredients list is non-empty and contains no meat/poultry/seafood."""
    if not ingredients:
        return False
    for ing in ingredients:
        name = ing.get('name', '').lower() if isinstance(ing, dict) else str(ing).lower()
        if any(kw in name for kw in _MEAT_KEYWORDS):
            return False
    return True
