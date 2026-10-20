"""Calorie estimation, TF-IDF recipe index and the prompt catalog builder."""

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from recipe_data import RECIPE_DB, _RECIPE_CUISINE, _RECIPE_MEAL_TYPES
from recipe_content import _MEAT_KEYWORDS


_SKIP_AMOUNT_WORDS = {
    "a", "an", "the", "cup", "cups", "tbsp", "tsp", "oz", "lb", "lbs",
    "g", "kg", "ml", "l", "handful", "bunch", "pinch", "dash", "clove",
    "cloves", "can", "package", "pkg", "slice", "slices", "piece", "pieces",
    "small", "medium", "large", "whole",
}


def _estimate_recipe_calories(recipe: dict) -> int:
    """
    Returns calories per serving. Uses stored real data when available,
    otherwise estimates from meal type and ingredient profile.
    """
    if "calories_per_serving" in recipe:
        return recipe["calories_per_serving"]

    meal_types = _RECIPE_MEAL_TYPES.get(recipe["_id"], ["dinner"])
    keywords = set(recipe.get("_keywords", []))
    ing_text = " ".join(recipe["base"].get("ingredients", [])).lower()

    cal = 420 if "breakfast" in meal_types else 620

    # Protein source
    if keywords & {"beef", "pork", "lamb", "bison"}:
        cal += 110
    elif keywords & {"chicken", "turkey"}:
        cal += 30
    elif keywords & {"salmon", "tuna", "shrimp", "fish", "seafood"}:
        cal += 10
    elif keywords & {"tofu", "tempeh", "lentil", "bean", "chickpea", "lentils", "beans"}:
        cal -= 70

    # Carb load
    if any(k in ing_text for k in ["pasta", "noodle", "spaghetti", "fettuccine", "penne", "rigatoni"]):
        cal += 170
    elif any(k in ing_text for k in [" rice", "quinoa", "farro", "couscous", "orzo"]):
        cal += 110
    elif any(k in ing_text for k in ["bread", "tortilla", "bun", "pita", "flatbread", "wrap"]):
        cal += 90
    elif any(k in ing_text for k in ["potato", "sweet potato"]):
        cal += 70

    # Richness / fat
    if any(k in ing_text for k in ["heavy cream", "coconut cream", "coconut milk", "full-fat"]):
        cal += 90
    if any(k in ing_text for k in ["parmesan", "mozzarella", "cheddar", "feta", "gruyere", "brie"]):
        cal += 60
    if any(k in ing_text for k in [" butter", "ghee"]) and "peanut butter" not in ing_text:
        cal += 40

    # Lighter profiles
    if keywords & {"salad"} or "salad" in recipe["base"]["title"].lower():
        cal -= 110

    return round(cal / 25) * 25


_recipe_vectorizer: TfidfVectorizer | None = None

_recipe_vectors = None   # scipy sparse matrix

_recipe_vector_ids: list[str] = []


def _build_recipe_vectors() -> None:
    """Build TF-IDF index over all recipes. Called once at module load."""
    global _recipe_vectorizer, _recipe_vectors, _recipe_vector_ids

    texts, ids = [], []
    for recipe in RECIPE_DB:
        rid = recipe["_id"]
        cal = _estimate_recipe_calories(recipe)
        cal_tier = "light" if cal < 450 else "hearty" if cal > 700 else "moderate"
        cuisine = _RECIPE_CUISINE.get(rid, "")
        meal_types = _RECIPE_MEAL_TYPES.get(rid, ["dinner"])
        keywords = recipe.get("_keywords", [])
        # Extract ingredient core names (skip numeric prefixes and unit words)
        ing_names = []
        for ing in recipe["base"].get("ingredients", [])[:8]:
            parts = ing.lower().split()
            start = 0
            while start < len(parts) and (parts[start][0].isdigit() or parts[start] in _SKIP_AMOUNT_WORDS):
                start += 1
            ing_names.append(" ".join(parts[start:start+3]))

        text = (
            f"{recipe['base']['title']} {cuisine} {' '.join(meal_types)} "
            f"{' '.join(keywords)} {' '.join(ing_names)} {cal_tier} {cal}kcal"
        )
        texts.append(text)
        ids.append(rid)

    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)
    _recipe_vectors = vec.fit_transform(texts)
    _recipe_vectorizer = vec
    _recipe_vector_ids = ids


def _semantic_recipe_ids(query: str, n: int = 30, exclude_ids: set = None) -> list[str]:
    """Return top-n recipe IDs ranked by TF-IDF cosine similarity to query."""
    if _recipe_vectorizer is None or _recipe_vectors is None:
        return [r["_id"] for r in RECIPE_DB[:n]]
    q_vec = _recipe_vectorizer.transform([query.lower()])
    sims = cosine_similarity(q_vec, _recipe_vectors)[0]
    ranked = sorted(enumerate(sims), key=lambda x: -x[1])
    result: list[str] = []
    for idx, _score in ranked:
        rid = _recipe_vector_ids[idx]
        if exclude_ids and rid in exclude_ids:
            continue
        result.append(rid)
        if len(result) >= n:
            break
    return result


# Build index immediately (RECIPE_DB and lookup dicts are already populated)
_build_recipe_vectors()


def _build_recipe_catalog(dietary_needs: str = "", selected_cuisines: list = None, hidden_recipe_ids: list = None, max_recipes: int = None, allowed_ids: set = None) -> str:
    """Build a compact catalog of DB recipes for the model to select from."""
    import random as _random
    dn = dietary_needs.lower()
    is_veg = any(w in dn for w in ["vegetarian", "vegan", "plant-based"])
    is_vegan = "vegan" in dn
    is_gf = any(w in dn for w in ["gluten-free", "gluten free", "celiac"])
    _VEGAN_EXCL = {"eggs", "dairy", "cheese", "milk", "yogurt", "butter", "cream", "honey"}
    _hidden = set(hidden_recipe_ids or [])

    lines = []
    for recipe in RECIPE_DB:
        rid = recipe["_id"]
        title = recipe["base"]["title"]
        meal_types = _RECIPE_MEAL_TYPES.get(rid, ["dinner"])
        keywords = set(recipe.get("_keywords", []))
        cuisine = _RECIPE_CUISINE.get(rid, "")

        if rid in _hidden:
            continue

        # Semantic filter: only include pre-selected IDs if provided
        if allowed_ids is not None and rid not in allowed_ids:
            continue

        # Dietary filtering
        if is_veg and keywords & _MEAT_KEYWORDS:
            continue
        if is_vegan and (keywords & _MEAT_KEYWORDS or keywords & _VEGAN_EXCL):
            continue

        # Hard-filter to selected cuisines to keep prompt small
        if selected_cuisines and cuisine and cuisine not in selected_cuisines:
            continue
        cuisine_tag = f" [{cuisine}]" if cuisine else ""

        cal = _estimate_recipe_calories(recipe)
        base_srv = recipe.get("servings")
        srv_tag = f" {base_srv}srv" if base_srv else ""
        variations = [u["name"] for u in recipe.get("uses", [])][:3]
        mt_str = "/".join(meal_types)
        var_str = " | ".join(variations)
        lines.append(f"[{rid}] {title}{cuisine_tag} ({mt_str}{srv_tag}) {cal}cal  {var_str}")

    # Trim to max_recipes while keeping a balanced breakfast/main split
    if max_recipes and len(lines) > max_recipes:
        breakfast_lines = [l for l in lines if "(breakfast)" in l]
        main_lines = [l for l in lines if "(breakfast)" not in l]
        n_b = min(len(breakfast_lines), max(4, max_recipes // 6))
        n_m = max_recipes - n_b
        _random.shuffle(breakfast_lines)
        _random.shuffle(main_lines)
        lines = breakfast_lines[:n_b] + main_lines[:n_m]
        _random.shuffle(lines)

    return "\n".join(lines)
