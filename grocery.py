"""Grocery-list building from planned recipes."""

import re
from calendar_utils import _build_calendar, _scale_amount
from nutrition import _STRIP_WORDS, _parse_ingredient
from recipe_content import _normalize_ingredient


_CAT_ORDER = ["Produce", "Meat & Seafood", "Dairy & Eggs", "Bakery", "Frozen", "Pantry"]

_VALID_CATS = set(_CAT_ORDER)

_OPTIONAL_MARKERS = {"optional", "garnish", "for serving", "to taste", "for topping", "for drizzling", "for finishing"}


def _categorize_ingredient(name: str) -> str:
    n = name.lower()
    # Compound pantry terms first — prevent "fish sauce", "peanut butter",
    # "coconut cream" etc. from being caught by meat/dairy/produce checks
    _COMPOUND_PANTRY = ["fish sauce","oyster sauce","hoisin sauce","worcestershire sauce",
                        "hot sauce","soy sauce","sesame oil","peanut butter","almond butter",
                        "nut butter","coconut milk","coconut cream","coconut water",
                        "almond milk","oat milk","soy milk","rice milk"]
    if any(k in n for k in _COMPOUND_PANTRY): return "Pantry"
    if any(k in n for k in ["chicken","beef","pork","turkey","lamb","salmon","tuna","shrimp","fish","steak","sausage","bacon","anchovy","cod","halibut","tilapia","mahi","sea bass","crab","lobster","scallop","clam","mussel","ground meat","ground beef","ground turkey","prosciutto","pancetta","chorizo","duck","venison","bison"]): return "Meat & Seafood"
    if any(k in n for k in ["butter","egg","milk","cream","yogurt","cheese","feta","parmesan","mozzarella","ricotta","brie","cheddar","gouda","kefir","ghee","half and half","sour cream","cottage cheese","mascarpone","gruyere","goat cheese"]): return "Dairy & Eggs"
    if "frozen" in n: return "Frozen"
    if any(k in n for k in ["bread","baguette","tortilla","pita","croissant","sourdough","bun","roll","wrap","naan","crouton","bagel","focaccia","ciabatta","brioche"]): return "Bakery"
    # Pantry before produce — catches oils, starches, spices that contain produce words
    if any(k in n for k in ["oil","vinegar","sauce","paste","powder","starch","flour","sugar","salt","syrup","broth","stock","extract","baking","soda","yeast","lard","shortening","mayo","mayonnaise","mustard","ketchup","honey","molasses","jam","jelly","preserve","pickle","canned","raisin","currant","tahini","miso","hummus","sriracha","gochujang","dried fruit"]): return "Pantry"
    if any(k in n for k in ["lettuce","spinach","kale","arugula","broccoli","carrot","tomato","cucumber","zucchini","pepper","onion","garlic","ginger","celery","mushroom","avocado","lemon","lime","orange","apple","berry","herb","cilantro","parsley","basil","mint","thyme","rosemary","scallion","green onion","shallot","leek","potato","sweet potato","butternut","squash","eggplant","asparagus","green bean","corn","pea","beet","radish","cabbage","bok choy","fennel","artichoke","mango","pineapple","coconut","fig","peach","plum","cherry","grape","banana","watermelon","jalapeño","serrano","habanero","chili","cauliflower","broccolini","rapini","endive","radicchio","watercress","chard","collard","turnip","parsnip","rutabaga","kohlrabi","daikon","edamame"]): return "Produce"
    return "Pantry"


def _raw_ing_to_structured(raw: str, scale: float) -> dict | None:
    """Parse a RECIPE_DB ingredient string and return a structured grocery dict, or None to skip."""
    if not isinstance(raw, str):
        return None
    clean = raw.strip().lstrip("+-•* ").strip()
    # Skip section headers like "For the coconut sauce:" or any line ending with ":"
    if clean.endswith(":") or re.match(r"^for\b", clean, re.IGNORECASE):
        return None
    p = _parse_ingredient(clean)
    name = p["food_name"]
    if not name:
        return None
    qty = p["qty"]
    unit = p["unit"] or ""
    if qty is not None:
        scaled = qty * scale
        amt_str = str(int(scaled)) if scaled == int(scaled) else f"{scaled:.2g}"
    else:
        amt_str = ""
    optional = any(marker in clean.lower() for marker in _OPTIONAL_MARKERS)
    return {
        "name": name,
        "amount": amt_str,
        "unit": unit,
        "category": _categorize_ingredient(name),
        "optional": optional,
        "userHas": False,
    }


_FRAC_MAP = {"¼": "0.25", "½": "0.5", "¾": "0.75", "⅓": "0.333", "⅔": "0.667", "⅛": "0.125", "⅜": "0.375", "⅝": "0.625"}

_VOLUME_UNITS = {"cup", "cups", "tbsp", "tsp", "tablespoon", "tablespoons", "teaspoon", "teaspoons",
                 "oz", "ounce", "ounces", "lb", "lbs", "pound", "pounds", "g", "gram", "grams",
                 "ml", "liter", "liters", "pint", "pints", "quart", "quarts",
                 "clove", "cloves", "slice", "slices", "large", "medium", "small",
                 "package", "pkg", "can", "stick", "sticks"}


def _parse_dessert_ingredient(ing_str: str) -> tuple:
    """Parse '2 cups flour' → (qty_float_or_None, unit_str, base_name_str)."""
    s = ing_str.strip()
    for f, v in _FRAC_MAP.items():
        s = s.replace(f, v)
    m = re.match(r'^(\d+(?:[./]\d+)?)\s+(\S+)\s+(.+)$', s)
    if m:
        raw_qty, token, rest = m.group(1), m.group(2).lower(), m.group(3).split(",")[0].strip()
        try:
            qty = float(raw_qty) if "/" not in raw_qty else float(raw_qty.split("/")[0]) / float(raw_qty.split("/")[1])
        except ValueError:
            qty = None
        if token in _VOLUME_UNITS:
            return qty, token, rest
        return qty, "", (token + " " + rest)
    m = re.match(r'^(\d+(?:\.\d+)?)\s+(.+)$', s)
    if m:
        try:
            qty = float(m.group(1))
        except ValueError:
            qty = None
        return qty, "", m.group(2).split(",")[0].strip()
    return None, "", s.split(",")[0].strip()


def _categorize_dessert_ingredient(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ["butter","egg","milk","cream","yogurt","cheese","custard","ricotta","mascarpone"]): return "Dairy & Eggs"
    if any(k in n for k in ["lemon","lime","orange","apple","banana","berry","berries","peach","cherry","strawberry","blueberry","raspberry","mango","pear","fig","date","raisin","zest","fruit"]): return "Produce"
    if any(k in n for k in ["flour","bread crumb","croissant","biscuit","pastry","puff pastry","phyllo","brioche"]): return "Bakery"
    if any(k in n for k in ["frozen"]): return "Frozen"
    return "Pantry"


def _build_grocery_by_recipe(accepted: list) -> list:
    """Return ingredients grouped by cluster for the 'by recipe' view."""
    result = []
    for cluster in accepted:
        items = []
        for ing in cluster.get("ingredients", []):
            if not ing.get("userHas", False):
                items.append({**ing, "key": _normalize_ingredient(ing.get("name", ""))})
        if items:
            meal_types = list({m.get("mealType", "dinner") for m in cluster.get("meals", []) if m.get("mealType")})
            result.append({
                "name": cluster.get("clusterName", ""),
                "meal_type": meal_types[0] if len(meal_types) == 1 else "mixed",
                "lines": items,
            })
    return result


def _clean_ingredient_name(name: str) -> str:
    """Strip prep/descriptor words from a stored ingredient name (handles old cached data)."""
    words = name.split()
    clean = [w for w in words if w.lower() not in _STRIP_WORDS]
    return " ".join(clean).strip() or name


def _build_grocery_data(accepted: list, meal_prep_days: list, planner: dict = None) -> tuple:
    """Return (raw_items, grocery_by_category) for a list of accepted clusters."""
    _, meal_counts = _build_calendar(accepted, meal_prep_days, planner=planner)
    grocery_map: dict = {}
    for cluster in accepted:
        cluster_meals = [m for m in cluster.get("meals", []) if m.get("name")]
        n_meals = len(cluster_meals)
        scale = max(1, round(sum(meal_counts.get((cluster["id"], m["name"]), 1) for m in cluster_meals) / n_meals)) if n_meals else 1
        for ing in cluster.get("ingredients", []):
            if not ing.get("userHas", False):
                display_name = _clean_ingredient_name(ing["name"])
                key = _normalize_ingredient(display_name)
                if key not in grocery_map:
                    grocery_map[key] = {**ing, "name": display_name, "key": key,
                                        "amount": _scale_amount(str(ing.get("amount", "")), scale),
                                        "optional": ing.get("optional", False)}
                elif not ing.get("optional", False):
                    grocery_map[key]["optional"] = False
    raw_items = list(grocery_map.values())
    by_cat: dict = {}
    for item in raw_items:
        cat = item.get("category", "Pantry") if item.get("category", "Pantry") in _VALID_CATS else "Pantry"
        by_cat.setdefault(cat, []).append(item)
    return raw_items, {c: by_cat[c] for c in _CAT_ORDER if c in by_cat}
