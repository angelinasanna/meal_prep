from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import anthropic
import asyncio
import calendar as _cal
import hashlib
import json
import math
from supabase import create_client, Client as SupabaseClient
import os
import random
import re
import secrets
import uuid
from collections import defaultdict
from datetime import date, timedelta
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

# ── Server-side cluster store (avoids cookie size limits) ───────────────────
_cluster_store: dict = {}
_recipe_store: dict = {}  # session_key -> generated recipe content
_cluster_locks: dict = {}  # session_key -> asyncio.Lock (guards concurrent per-meal-type writes)


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
    _cluster_store[_store_key(request)] = clusters


def clear_clusters(request: Request):
    sk = _store_key(request)
    _cluster_store.pop(sk, None)
    _cluster_locks.pop(sk, None)


# ── Cuisine / season constants ───────────────────────────────────────────────
CUISINES = [
    "Italian", "Mexican", "Japanese", "Chinese",
    "Indian", "Thai", "Korean", "French",
    "Greek", "American", "Spanish", "Vietnamese",
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
    "Summer": "seasonal summer produce (tomatoes, zucchini, corn, peppers, basil, stone fruits, cucumbers, berries). Fresh, lighter meals [ grilling works well.",
    "Fall":   "seasonal fall produce (butternut squash, sweet potato, apples, Brussels sprouts, mushrooms, root vegetables). Warming, hearty dishes.",
    "Winter": "seasonal winter produce (citrus, kale, root vegetables, cabbage, pears, dried legumes). Comforting soups, stews, and braises.",
}

_ADVENTURE_GUIDANCE = {
    "familiar": (
        "Simple, classic recipes. Ingredients must be basic everyday staples found in any grocery store: "
        "chicken, ground beef, eggs, pasta, rice, potatoes, common vegetables (broccoli, zucchini, carrots, "
        "spinach, tomatoes, onion, garlic), standard pantry items (olive oil, butter, salt, pepper, soy sauce, "
        "lemon). No specialty varieties, no unusual items, no hard-to-find ingredients whatsoever."
    ),
    "curious": (
        "More interesting recipes but still accessible ingredients ] things any grocery store stocks: "
        "dijon mustard, capers, sun-dried tomatoes, fresh herbs, greek yogurt, sriracha, rice vinegar, "
        "tahini, smoked paprika, cumin, feta. No specialty imports or hard-to-source items."
    ),
    "bold": (
        "Complex recipes with specialty and global ingredients: mirin, fish sauce, miso, harissa, cotija, "
        "nori, gochujang, preserved lemon, sumac, za'atar, pomegranate molasses, tamarind, etc. "
        "Explore lesser-known cuisines and advanced techniques."
    ),
}

# ── Curated recipes per cuisine [ injected into AI prompt when that cuisine is selected ──
_RECIPE_MEAL_TYPES: dict[str, list[str]] = {
    # Breakfast
    "breakfast-taco-bowl":   ["breakfast"],
    "berry-parfait":         ["breakfast"],
    "overnight-oats":        ["breakfast"],
    "protein-overnight-oats": ["breakfast"],
    "sausage-egg-bites":     ["breakfast"],
    "breakfast-burritos":    ["breakfast"],
    "bacon-gruyere-egg-bites": ["breakfast"],
    "protein-chia-pudding":  ["breakfast"],
    "cosmic-brownie-oats":   ["breakfast"],
    "cottage-cheese-bowl":   ["breakfast"],
    "crustless-veggie-quiche": ["breakfast"],
    "sheet-pan-eggs":        ["breakfast"],
    "breakfast-snack-plate": ["breakfast"],
    "gourmet-toast":         ["breakfast"],
    "savory-gourmet-toast":  ["breakfast"],
    "salade-nicoise":        ["lunch"],
    "sweet-crepes":          ["breakfast"],
    "savory-crepes":         ["breakfast"],
    "korean-beef-bowls":     ["lunch"],
    "kimchi-fried-rice":     ["lunch"],
    "korean-chicken-bowls":  ["lunch", "dinner"],
    "korean-shrimp-bowls":   ["dinner"],
    "korean-spicy-tofu":     ["dinner"],
    # Lunch only
    "dan-dan-noodles":       ["lunch"],
    "beef-fajitas":          ["lunch"],
    "chicken-katsudon":      ["lunch"],
    "buddha-bowl":           ["lunch"],
    "tuna-sushi":            ["lunch"],
    "teriyaki-salmon-udon":  ["dinner"],
    "beef-barbacoa":         ["lunch"],
    "kale-salad-base":       ["lunch"],
    "bean-veggie-salad":     ["lunch"],
    "italian-couscous":      ["lunch"],
    "chicken-burrito-bowl":  ["lunch"],
    "guac-stuffed-peppers":  ["lunch"],
    "cheesy-chicken-orzo":   ["dinner"],
    "french-lentil-soup":    ["dinner"],
    # Dinner only
    "miso-maple-chicken":    ["dinner"],
    "harissa-salmon":        ["dinner"],
    "turmeric-salmon":       ["dinner"],
    "pistachio-pesto-salmon": ["dinner"],
    "lemon-salmon":          ["dinner"],
    "herby-parmesan-meatballs": ["dinner"],
    "walder-shrimp":         ["dinner"],
    "walder-tofu":           ["dinner"],
    "hot-honey-zaatar-turkey": ["dinner"],
    "broccoli-pasta":        ["dinner"],
    "tortellini-soup":       ["dinner"],
    "chicken-parm-meatballs": ["dinner"],
    "tuscan-chicken-pasta":  ["dinner"],
    "pasta-alla-gricia":     ["dinner"],
    "ground-turkey-base":    ["dinner"],
    "greek-meatball-bowls":    ["lunch", "dinner"],
    "greek-chicken-bowl":      ["lunch", "dinner"],
    "mediterranean-lamb-bowl": ["dinner"],
    "greek-breakfast-bowl":    ["breakfast"],
    "greek-scrambled-eggs":    ["breakfast"],
    "greek-egg-bites":         ["breakfast"],
    "shakshuka":               ["breakfast"],
    "turkish-eggs":            ["breakfast"],
    "ground-turkey-pita":        ["lunch"],
    "chicken-tandoori-bowls":   ["lunch"],
    "vegan-indian-curry":        ["dinner"],
    "paneer-tikka-bowl":         ["lunch", "dinner"],
    "zucchini-chickpea-curry":   ["dinner"],
    "coconut-chicken-curry":     ["dinner"],
    "coconut-lentil-curry":      ["dinner"],
    "indian-veggie-rice-bowl":   ["lunch"],
    "indian-savory-toast":       ["breakfast"],
    "spanish-breakfast-hash":    ["breakfast"],
    "spanish-scrambled-eggs":    ["breakfast"],
    "spanish-tortilla":          ["breakfast"],
    "spanish-tortilla-muffins":  ["breakfast"],
    "spanish-chicken":           ["dinner"],
    "gambas-al-ajillo":          ["dinner"],
    "spanish-garlic-soup":       ["dinner"],
    "spanish-beef-rice":         ["lunch"],
    "chipotle-rice-bowl":        ["lunch"],
    "mexican-chorizo-casserole": ["breakfast"],
    "mexican-street-corn":       ["lunch"],
    "wonton-soup":           ["dinner"],
    "honey-soy-chicken":     ["dinner"],
    "peanut-chicken-noodle-bowls":    ["lunch"],
    "crispy-pork-banh-mi":            ["dinner"],
    "lemongrass-chicken-rolls":       ["lunch"],
    "vietnamese-pork-noodle-bowls":   ["dinner"],
    "vietnamese-chicken-salad":       ["lunch"],
    "pho-saigon":                     ["dinner"],
    "tom-rim-shrimp":                 ["dinner"],
    "canh-chua-ca":                   ["dinner"],
    "grilled-pork-rice-paper-rolls":  ["lunch"],
}

_CUISINE_RECIPE_LIBRARY: dict[str, list[str]] = {
    "Italian": [
        "Pasta con i Broccoli ] silky anchovy-garlic broccoli pasta; the same broccoli base also yields a Cream of Broccoli Soup (vellutata) and Crispy Toasted Breadcrumbs (pangrattato)",
        "Spicy Italian Sausage and Tortellini Soup [ one-pot with fire-roasted tomatoes, kale, cheese tortellini, and a swirl of cream",
        "Italian Couscous Salad ] roasted garlic couscous with salami, bocconcini, chickpeas, olives, cherry tomatoes, and red wine vinaigrette",
        "Spicy Tuscan Chicken Pasta [ pan-seared chicken with sun-dried tomatoes, baby spinach, and a creamy Parmesan sauce over penne",
    ],
    "Japanese": [
        "Japanese Buddha Bowl ] mixed grains, baked tofu, roasted sweet potato, romaine, avocado, carrots, cucumber, corn, and wakame with a creamy sesame dressing; swap components for different bowl combos",
        "Miso Maple Chili Crisp Chicken [ sticky miso-maple glazed chicken thighs with chili crisp; serves as a rice bowl, noodle bowl, or lemony kale salad",
        "Chicken Katsu Don ] panko-crusted chicken cutlet over steamed rice with a dashi-soy egg sauce; same katsu works as a sando sandwich or a katsu salad",
        "Teriyaki Salmon Udon [ soy-mirin glazed salmon over udon noodles with snap peas and edamame",
        "Tuna Sushi ] sushi rice with tuna rolled into maki or served as a deconstructed tuna rice bowl with avocado, cucumber, and soy",
    ],
    "Chinese": [
        "Pork & Shrimp Wonton Soup [ hand-folded wontons with ground pork and shrimp in a clear ginger-sesame broth; same wontons served as a rice bowl with chili oil or in a spicy miso ramen",
        "Dan Dan Noodles ] ground pork with Sichuan preserved mustard greens and fermented black beans in a sesame-chili sauce; served hot, chilled as a cold noodle salad, or over smashed cucumber",
        "Honey Soy Chicken [ soy-ginger marinated wings (or breast) glazed with honey and sesame; served over rice, with wok-blistered green beans, or with garlic bok choy",
    ],
    "Mexican": [
        "Chicken Burrito Bowl ] cumin-spiced baked chicken over cilantro rice with avocado, cherry tomatoes, black olives, cheddar, and salsa; same base wraps into a burrito or lettuce wrap",
        "Ground Turkey [ seasoned with taco spices and cooked with onion, bell peppers, carrots, mushrooms, and jalapeño; serves as taco filling, stuffed bell peppers, or a hearty taco soup",
        "Guacamole Stuffed Mini Peppers ] simple lime guacamole stuffed into mini pepper halves; the same guac base makes nachos or a snack plate with carrots and peppers",
        "Beef Fajitas [ taco-seasoned stir-fry beef with sautéed bell peppers and red onion; serves as a fajita bowl, tacos, or a fajita salad",
        "Beef Barbacoa ] chipotle and cumin braised chuck roast shredded and served over quinoa; works as a barbacoa bowl or a barbacoa salad",
    ],
}

_RECIPE_CUISINE: dict[str, str] = {
    "broccoli-pasta":          "Italian",
    "tortellini-soup":         "Italian",
    "italian-couscous":        "Italian",
    "herby-parmesan-meatballs": "Italian",
    "chicken-parm-meatballs":  "Italian",
    "tuscan-chicken-pasta":    "Italian",
    "pasta-alla-gricia":       "Italian",
    "chicken-burrito-bowl":    "Mexican",
    "ground-turkey-base":      "Mexican",
    "guac-stuffed-peppers":    "Mexican",
    "beef-fajitas":            "Mexican",
    "beef-barbacoa":           "Mexican",
    "breakfast-taco-bowl":     "Mexican",
    "breakfast-burritos":      "Mexican",
    "miso-maple-chicken":      "Japanese",
    "chicken-katsudon":        "Japanese",
    "tuna-sushi":              "Japanese",
    "teriyaki-salmon-udon":    "Japanese",
    "buddha-bowl":             "Japanese",
    "walder-tofu":             "Japanese",
    "wonton-soup":             "Chinese",
    "dan-dan-noodles":         "Chinese",
    "honey-soy-chicken":       "Chinese",
    "harissa-salmon":          "American",
    "turmeric-salmon":         "American",
    "pistachio-pesto-salmon":  "American",
    "lemon-salmon":            "Mediterranean",
    "berry-parfait":           "Mediterranean",
    "overnight-oats":          "American",
    "kale-salad-base":         "American",
    "walder-shrimp":           "American",
    "hot-honey-zaatar-turkey": "French",
    "bean-veggie-salad":       "American",
    "protein-overnight-oats":  "American",
    "sausage-egg-bites":       "American",
    "bacon-gruyere-egg-bites": "French",
    "protein-chia-pudding":    "American",
    "cosmic-brownie-oats":     "American",
    "cottage-cheese-bowl":     "American",
    "crustless-veggie-quiche":       "French",
    "cheesy-chicken-orzo":           "French",
    "salade-nicoise":                "French",
    "french-lentil-soup":            "French",
    "savory-crepes":                 "French",
    "sweet-crepes":                  "French",
    "korean-beef-bowls":             "Korean",
    "kimchi-fried-rice":             "Korean",
    "korean-shrimp-bowls":           "Korean",
    "korean-chicken-bowls":          "Korean",
    "korean-spicy-tofu":             "Korean",
    "sheet-pan-eggs":          "American",
    "breakfast-snack-plate":   "American",
    "gourmet-toast":           "American",
    "savory-gourmet-toast":    "American",
    "greek-meatball-bowls":    "Mediterranean",
    "greek-chicken-bowl":      "Mediterranean",
    "mediterranean-lamb-bowl": "Mediterranean",
    "greek-breakfast-bowl":    "Mediterranean",
    "greek-scrambled-eggs":    "Mediterranean",
    "greek-egg-bites":         "Mediterranean",
    "shakshuka":               "Mediterranean",
    "turkish-eggs":            "Mediterranean",
    "ground-turkey-pita":        "Mediterranean",
    "chicken-tandoori-bowls":   "Indian",
    "vegan-indian-curry":        "Indian",
    "paneer-tikka-bowl":         "Indian",
    "zucchini-chickpea-curry":   "Indian",
    "coconut-chicken-curry":     "Indian",
    "coconut-lentil-curry":      "Indian",
    "indian-veggie-rice-bowl":   "Indian",
    "indian-savory-toast":       "Indian",
    "spanish-breakfast-hash":    "Spanish",
    "spanish-scrambled-eggs":    "Spanish",
    "spanish-tortilla":          "Spanish",
    "spanish-tortilla-muffins":  "Spanish",
    "spanish-chicken":           "Spanish",
    "gambas-al-ajillo":          "Spanish",
    "spanish-garlic-soup":       "Spanish",
    "spanish-beef-rice":         "Spanish",
    "chipotle-rice-bowl":        "Mexican",
    "mexican-chorizo-casserole": "Mexican",
    "mexican-street-corn":       "Mexican",
    "peanut-chicken-noodle-bowls":    "Vietnamese",
    "crispy-pork-banh-mi":            "Vietnamese",
    "lemongrass-chicken-rolls":       "Vietnamese",
    "vietnamese-pork-noodle-bowls":   "Vietnamese",
    "vietnamese-chicken-salad":       "Vietnamese",
    "pho-saigon":                     "Vietnamese",
    "tom-rim-shrimp":                 "Vietnamese",
    "canh-chua-ca":                   "Vietnamese",
    "grilled-pork-rice-paper-rolls":  "Vietnamese",
}

# ── Recipe database (Good Mood Food newsletter + curated Italian) ─────────────
# Each entry has private _id and _keywords fields (stripped before returning),
# plus the exact schema that _generate_one_recipe() returns.
RECIPE_DB = [
    {
        "_id": "miso-maple-chicken",
        "_keywords": ["chicken", "miso"],
        "image": "/static/images/miso-maple-chicken.jpg",
        "intro": "One sticky, savory marinade does all the work [ cook the chicken and vegetables together, then eat from it three different ways across the week.",
        "base": {
            "title": "Miso Maple Chili Crisp Chicken",
            "ingredients": [
                "3.5-4 lbs boneless skinless chicken breasts and/or thighs",
                "1/4 cup white miso paste",
                "1/3 cup maple syrup",
                "1/4 cup low-sodium soy sauce",
                "2 tbsp chili crisp",
                "1/4 cup rice vinegar",
                "1 head broccoli (about 1 lb), broken into florets",
                "1 lb carrots, peeled and cut on the bias",
                "Olive oil, salt, and pepper",
            ],
            "steps": [
                "Preheat oven to 425 F.",
                "Whisk together miso paste, maple syrup, soy sauce, chili crisp, and rice vinegar. Set aside 1/4 cup of the marinade.",
                "Marinate chicken in the remaining marinade for 30 minutes in the refrigerator.",
                "Toss broccoli and carrots with olive oil and salt on a parchment-lined baking sheet.",
                "Place marinated chicken on a separate foil-lined sheet and spoon any remaining marinade on top.",
                "Bake chicken breasts 20 minutes (165 F internal), thighs 25 minutes. Roast vegetables about 25 minutes. Optional: broil thighs 2 minutes for extra browning.",
                "Rest chicken 5 minutes before slicing. Keep the reserved marinade for drizzling.",
            ],
        },
        "uses": [
            {
                "name": "Rice Bowl",
                "subtitle": "with pickled cucumber and avocado",
                "extras": [
                    "1 cup uncooked white rice",
                    "1 Persian cucumber, sliced",
                    "1/4 red onion, thinly sliced",
                    "Rice vinegar, for quick pickling",
                    "1 small avocado, sliced",
                    "Cilantro and sesame seeds, to finish",
                ],
                "steps": [
                    "Cook rice using your preferred method.",
                    "Quick-pickle cucumber and red onion in a splash of rice vinegar for at least 5 minutes.",
                    "Divide rice between bowls. Top with sliced chicken, pickled vegetables, roasted broccoli and carrots, and avocado.",
                    "Drizzle with reserved marinade and garnish with cilantro and sesame seeds.",
                ],
                "tip": None,
            },
            {
                "name": "Lemony Chopped Chicken Kale Salad",
                "subtitle": "with almonds and lemon dressing",
                "extras": [
                    "1 bunch curly kale, stems removed and chopped",
                    "1 avocado, diced",
                    "1 Persian cucumber, sliced",
                    "1/4 red onion, sliced",
                    "1 red bell pepper, chopped",
                    "1/3 cup sliced almonds",
                    "Juice of 2 lemons",
                    "1 tbsp rice vinegar",
                    "1 tbsp Dijon mustard",
                    "3 tbsp olive oil",
                ],
                "steps": [
                    "Place kale in a large bowl, drizzle with olive oil and a pinch of salt, and massage until softened.",
                    "Add chopped chicken, avocado, cucumber, red onion, bell pepper, and almonds.",
                    "Whisk together lemon juice, rice vinegar, Dijon, olive oil, salt, and pepper.",
                    "Toss salad with dressing. Top with cilantro and furikake if you have it.",
                ],
                "tip": "Kale holds up well in the fridge ] this is a great pack-ahead lunch.",
            },
            {
                "name": "Garlic and Ginger Chicken Soup",
                "subtitle": "with bok choy, mushrooms, and bone broth",
                "extras": [
                    "8 oz white mushrooms, sliced",
                    "2 bunches baby bok choy",
                    "1-inch knob fresh ginger, peeled and chopped",
                    "3 garlic cloves, chopped",
                    "4 cups chicken bone broth",
                    "2 tsp soy sauce",
                    "Sliced scallions, to finish",
                ],
                "steps": [
                    "Heat a splash of olive oil in a medium pot over medium-low. Cook ginger and garlic 1-2 minutes until fragrant.",
                    "Add mushrooms, bok choy, shredded chicken, roasted broccoli and carrots, broth, and soy sauce.",
                    "Simmer 5-10 minutes until mushrooms are tender and bok choy wilts.",
                    "Serve topped with sliced scallions and an extra drizzle of chili crisp.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "harissa-salmon",
        "_keywords": ["harissa", "salmon"],
        "image": "/static/images/Baked-Harissa-Salmon.jpg",
        "intro": "Light, fresh, and bold [ salmon coated in a simple harissa sauce and served over lemony quinoa with wilted kale and golden raisins. A 35-minute meal prep that works as three different plates across the week.",
        "base": {
            "title": "Baked Harissa Salmon with Lemon Quinoa",
            "ingredients": [
                "4 salmon fillets (4–6 oz each)",
                "1/4 cup mild harissa paste",
                "2 cloves garlic, minced",
                "2 tbsp olive oil",
                "1 cup quinoa, dry",
                "4 cups kale, shredded or torn",
                "1 tbsp olive oil (for kale)",
                "Juice and zest of 2 lemons",
                "6 tbsp golden raisins",
                "Salt and pepper",
            ],
            "steps": [
                "Preheat oven to 400 F.",
                "Cook quinoa with water according to package directions (about 15 minutes) until all liquid is absorbed.",
                "Whisk together harissa paste, minced garlic, and olive oil in a small bowl.",
                "Place salmon fillets in a baking dish, coat with the harissa mixture, and bake 15 minutes until cooked through.",
                "While salmon bakes, heat 1 tbsp olive oil in a pan over medium heat and sauté kale about 2 minutes until wilted.",
                "Stir wilted kale into the cooked quinoa along with lemon juice, zest, and golden raisins. Season with salt and pepper.",
            ],
        },
        "uses": [
            {
                "name": "Lemon Quinoa Bowl",
                "subtitle": "harissa salmon over lemon kale quinoa with golden raisins",
                "image": "/static/images/Baked-Harissa-Salmon.jpg",
                "extras": [],
                "steps": [
                    "Spoon lemon kale quinoa into a bowl.",
                    "Top with a harissa salmon fillet and serve immediately.",
                ],
                "tip": "The quinoa keeps 4 days refrigerated. Reheat with a splash of water so it stays fluffy.",
            },
            {
                "name": "Honey Broccolini Plate",
                "subtitle": "with miso tahini drizzle and honey-roasted kale",
                "extras": [
                    "2 bunches broccolini, ends trimmed",
                    "4 cups curly kale, torn",
                    "2 tsp honey",
                    "For miso tahini: 1/4 cup tahini, 2 tbsp miso paste, 6 tbsp nutritional yeast, 1/4 tsp garlic powder, 6 tbsp hot water",
                ],
                "steps": [
                    "Toss broccolini with olive oil, salt, and pepper. Roast at 350 F for 15 minutes.",
                    "Add kale, drizzle with olive oil, roast 5 more minutes, then finish with honey.",
                    "Blend miso tahini: combine tahini, miso, nutritional yeast, garlic powder, and hot water until smooth.",
                    "Plate salmon over the broccolini and kale. Drizzle miso tahini generously.",
                ],
                "tip": None,
            },
            {
                "name": "Green Bean Salad Plate",
                "subtitle": "with Dijon vinaigrette, toasted almonds, and fresh parsley",
                "extras": [
                    "1–1.5 lbs green beans, ends trimmed",
                    "2 cloves garlic, minced",
                    "1/2 cup almonds, chopped",
                    "1/4 cup fresh parsley, chopped",
                    "For vinaigrette: 1/4 cup olive oil, 1 tbsp white wine vinegar, 3 tsp Dijon, 1/2 tsp garlic powder, 1 tbsp lemon juice",
                ],
                "steps": [
                    "Shake all vinaigrette ingredients together in a jar.",
                    "Sauté green beans with olive oil and garlic over medium heat 10–12 minutes until tender-crisp.",
                    "Toast almonds in a dry pan 7–10 minutes until lightly golden.",
                    "Toss green beans with vinaigrette, almonds, and parsley. Lay salmon alongside.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "turmeric-salmon",
        "_keywords": ["turmeric", "salmon"],
        "image": "/static/images/Crispy-Turmeric-Salmon-With-Yogurt-Sauce.jpg",
        "intro": "Pan-seared with a golden turmeric crust and served with a cool herbed yogurt ] this salmon pairs beautifully with bold roasted vegetables.",
        "base": {
            "title": "Crispy Turmeric Salmon with Parsley Yogurt Sauce",
            "ingredients": [
                "4 salmon fillets (4-6 oz each)",
                "2 tbsp olive or avocado oil",
                "1 tsp turmeric powder",
                "1 tsp ground cumin",
                "1 tsp garlic powder",
                "Salt and pepper",
                "For yogurt sauce: 2/3 cup plain Greek yogurt, 1/2 cup fresh parsley, 1 tsp garlic powder, juice of 1 lemon, salt and pepper",
            ],
            "steps": [
                "Mix oil, turmeric, cumin, and garlic powder in a small bowl. Brush generously over salmon. Season with salt and pepper.",
                "Blend all yogurt sauce ingredients until smooth. Refrigerate until ready to serve.",
                "Heat a pan over high heat until very hot (a drop of water sizzles on contact).",
                "Place salmon skin-side down. Cook 3-5 minutes, pressing gently with a spatula.",
                "Once the sides turn opaque, flip. Turn off the heat and cook 1-2 more minutes. The interior should still be slightly tender.",
            ],
        },
        "uses": [
            {
                "name": "Crispy Potato Bowl",
                "subtitle": "chili-spiced potatoes, turmeric salmon, and parsley yogurt",
                "extras": [
                    "2 lbs mini potatoes, quartered",
                    "1 tbsp chili powder, 1 tsp paprika, 1 tsp garlic powder",
                    "Fresh parsley or cilantro, to finish",
                ],
                "steps": [
                    "Toss quartered potatoes with olive oil, chili powder, paprika, garlic powder, salt, and pepper.",
                    "Roast at 400 F for 25-30 minutes, stirring halfway, until golden and crispy.",
                    "Pile potatoes into a bowl, lay salmon on top, and spoon parsley yogurt sauce generously over everything.",
                ],
                "tip": "Reheat potatoes in the oven or a hot pan [ microwave makes them soft.",
            },
            {
                "name": "Spiced Cauliflower Plate",
                "subtitle": "curry-roasted cauliflower, cashews, raisins, and minty yogurt",
                "extras": [
                    "2 heads cauliflower, cut into 1-inch florets",
                    "3 tbsp olive oil, 1.5 tsp curry powder, 1 tsp garlic powder",
                    "1/4 cup cashews, 3 tbsp raisins",
                    "For minty yogurt: 3/4 cup Greek yogurt, 1 cup fresh mint, 3 tbsp lemon juice, 1 tsp garlic powder",
                ],
                "steps": [
                    "Toss cauliflower with olive oil, curry powder, garlic powder, and salt. Roast at 425 F for 25 minutes until browned.",
                    "Blend yogurt, mint, lemon juice, and garlic powder until smooth. Season with salt.",
                    "Plate salmon next to the cauliflower. Scatter cashews and raisins over the top and drizzle minty yogurt over both.",
                ],
                "tip": "The minty yogurt keeps for 3-4 days and works well on everything.",
            },
            {
                "name": "Zucchini & Farro Bowl",
                "subtitle": "parmesan zucchini, farro, and parsley yogurt",
                "extras": [
                    "1/2 cup dry farro",
                    "2 large zucchini, cut into 3-inch strips",
                    "1/2 tsp garlic powder and 1/2 tsp dried basil",
                    "1/4 cup Parmigiano-Reggiano, freshly grated",
                    "Fresh basil, to finish",
                ],
                "steps": [
                    "Cook farro according to package directions. Season with olive oil, salt, and pepper.",
                    "Toss zucchini with olive oil, garlic powder, dried basil, and salt. Spread on parchment and top with Parmesan.",
                    "Roast at 400 F for 20 minutes until fork-tender and cheese is golden.",
                    "Bowl up the farro and zucchini. Lay salmon on top and spoon parsley yogurt sauce over everything. Finish with fresh basil.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "pistachio-pesto-salmon",
        "_keywords": ["pistachio", "salmon"],
        "image": "/static/images/Baked-Salmon-With-Pistachio-Pesto.jpg",
        "intro": "A bright, nutty pistachio pesto takes minutes to blend and turns a simple baked salmon into something that feels special ] mix with different sides all week.",
        "base": {
            "title": "Baked Salmon with Pistachio Pesto",
            "ingredients": [
                "4 salmon fillets (4-6 oz each)",
                "1 tbsp extra virgin olive oil",
                "Salt and pepper",
                "For pistachio pesto: 2 cups fresh basil, 2 cloves garlic, 1/4 cup shelled pistachios, 1/3 cup nutritional yeast (or Parmesan), 1/3 cup extra virgin olive oil, salt and pepper",
            ],
            "steps": [
                "Preheat oven to 400 F. Line a baking sheet with parchment.",
                "Drizzle salmon with olive oil and season with salt and pepper.",
                "Bake 12 minutes until cooked through but still tender.",
                "While salmon bakes, blend all pesto ingredients in a food processor until smooth.",
                "Remove salmon from oven and spoon pesto generously over each fillet.",
            ],
        },
        "uses": [
            {
                "name": "Butternut Squash Bowl",
                "subtitle": "with kale, toasted almonds, and spicy coconut cream",
                "extras": [
                    "1 medium butternut squash, peeled and cut into 3/4-inch cubes",
                    "3 cloves garlic, minced",
                    "4 cups curly kale, torn",
                    "1/2 cup almonds, chopped",
                    "For coconut sauce: 1 cup canned coconut cream, 1 tsp red pepper flakes, 1 tsp cornstarch",
                ],
                "steps": [
                    "Toss squash with olive oil, garlic, and salt. Roast at 400 F for 30 minutes, stirring halfway.",
                    "Stir in kale and almonds, return to oven for 6 more minutes until kale is slightly crispy.",
                    "Simmer coconut cream, whisk in red pepper flakes and cornstarch, cook 10-15 minutes until thickened.",
                    "Bowl up the squash and kale. Drizzle coconut sauce over the top, then add salmon and a spoonful of pistachio pesto.",
                ],
                "tip": None,
            },
            {
                "name": "Zucchini Parmesan Plate",
                "subtitle": "golden parmesan zucchini with pistachio pesto over the top",
                "extras": [
                    "2 large zucchini, cut into 3-inch strips",
                    "1/2 tsp garlic powder and 1/2 tsp dried basil",
                    "1/4 cup Parmigiano-Reggiano, freshly grated",
                    "Fresh basil, to finish",
                ],
                "steps": [
                    "Toss zucchini with olive oil, garlic powder, dried basil, and salt. Spread on parchment lined with Parmesan on top.",
                    "Roast at 400 F for 20 minutes until fork-tender and cheese is golden.",
                    "Plate zucchini and lay salmon alongside. Spoon extra pistachio pesto over everything and scatter fresh basil on top.",
                ],
                "tip": "Pesto keeps in the fridge for a week [ spoon it on toast, pasta, or grain bowls all week.",
            },
            {
                "name": "Farro & Green Bean Bowl",
                "subtitle": "with Dijon vinaigrette, toasted almonds, and pistachio pesto",
                "extras": [
                    "1/2 cup dry farro",
                    "1-1.5 lbs green beans, ends trimmed",
                    "2 cloves garlic, minced",
                    "1/2 cup almonds, chopped",
                    "1/4 cup fresh parsley, chopped",
                    "For vinaigrette: 1/4 cup olive oil, 1 tbsp white wine vinegar, 3 tsp Dijon, 1 tbsp lemon juice",
                ],
                "steps": [
                    "Cook farro according to package directions. Season lightly with olive oil, salt, and pepper.",
                    "Saute green beans with olive oil and garlic over medium heat for 10-12 minutes until tender-crisp.",
                    "Toast almonds in a dry pan until golden. Shake vinaigrette ingredients together in a jar.",
                    "Toss green beans with vinaigrette, almonds, and parsley. Serve over farro with salmon and a generous scoop of pistachio pesto.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "lemon-salmon",
        "_keywords": ["salmon"],
        "image": "/static/images/salmon-three-ways.jpg",
        "intro": "Simple lemon-baked salmon that splits into three completely different meals ] a vibrant Mediterranean bowl, spicy hand rolls, and fresh fish tacos.",
        "base": {
            "title": "Lemon Garlic Salmon",
            "ingredients": [
                "2 3/4 lbs salmon fillet",
                "4 cloves garlic, finely chopped",
                "2 tbsp olive oil",
                "Juice of 1 lemon",
                "Salt and pepper",
                "Extra lemon slices for topping (optional)",
            ],
            "steps": [
                "Preheat oven to 400 F and line a baking sheet with parchment.",
                "Mix together garlic, olive oil, lemon juice, salt, and pepper.",
                "Place salmon on the sheet and brush the mixture all over. Add lemon slices on top if you like.",
                "Bake 12-15 minutes until salmon is opaque and flakes easily with a fork.",
            ],
        },
        "uses": [
            {
                "name": "Mediterranean Salmon Bowl",
                "subtitle": "with roasted peppers, tzatziki, and avocado-feta salad",
                "extras": [
                    "2 red bell peppers",
                    "1 zucchini, diced",
                    "1 cup rice, seasoned with 1 tsp cumin and juice of 1 lemon",
                    "1 avocado, diced",
                    "1/3 cup crumbled feta",
                    "1 1/2 tbsp fresh dill",
                    "1 cup Greek yogurt (for tzatziki)",
                    "2 Persian cucumbers (one grated for tzatziki)",
                    "2 garlic cloves, minced",
                    "Quick-pickled red onion",
                ],
                "steps": [
                    "Roast peppers and zucchini at 400 F for 20-25 minutes, stirring halfway. Peel and slice the peppers.",
                    "Make tzatziki: grate one cucumber, squeeze dry, then mix with yogurt, garlic, dill, a splash of vinegar, and salt.",
                    "Quick-pickle red onion by pouring boiling sweetened vinegar over thin slices and letting it cool.",
                    "Cook rice and stir in cumin, lemon juice, and salt.",
                    "Build each bowl: seasoned rice, salmon, roasted vegetables, avocado-feta salad (toss avocado with feta, dill, and lemon), pickled onions, and a dollop of tzatziki.",
                ],
                "tip": None,
            },
            {
                "name": "Spicy Salmon Hand Rolls",
                "subtitle": "with sriracha mayo, avocado, and cucumber",
                "extras": [
                    "5 nori sheets, halved",
                    "3 tbsp mayonnaise",
                    "1 1/2 tbsp sriracha",
                    "1 tsp soy sauce",
                    "1 1/2 cups cooked rice, seasoned with 2 tbsp rice vinegar, 1/2 tsp salt, 1/2 tsp sugar",
                    "1 avocado, thinly sliced",
                    "1 Persian cucumber, cut into matchsticks",
                    "Sesame seeds and microgreens",
                ],
                "steps": [
                    "Mash the salmon with sriracha, mayo, soy sauce, and sliced green onions.",
                    "Lay a nori half shiny-side down. Spread a thin layer of seasoned rice on one end.",
                    "Top with a spoonful of spicy salmon, sesame seeds, avocado slices, cucumber, and microgreens.",
                    "Roll from the bottom-left corner diagonally up. Wet a fingertip to seal the edge.",
                ],
                "tip": "Assemble these just before eating [ nori gets soggy fast.",
            },
            {
                "name": "Salmon Tacos with Mango Avocado Salsa",
                "subtitle": "with tzatziki and pickled onions",
                "extras": [
                    "4 flour tortillas",
                    "1 ripe mango, finely chopped",
                    "1 avocado, chopped",
                    "2 tbsp cilantro and 1 tbsp fresh mint",
                    "Juice of 1 lime",
                    "1 tsp taco seasoning",
                ],
                "steps": [
                    "Mix together mango, avocado, cilantro, mint, lime juice, and salt to make the salsa.",
                    "Rub taco seasoning onto the salmon. Sear in a hot oiled skillet 1-2 minutes per side until crispy and warmed through.",
                    "Warm tortillas in a dry skillet.",
                    "Spread tzatziki on each tortilla. Top with salmon, mango salsa, and pickled onions.",
                    "Serve with extra lime wedges and cilantro.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "herby-parmesan-meatballs",
        "_keywords": ["meatball"],
        "image": "/static/images/pearl-halloumi-bowl.jpg",
        "intro": "A big batch of herby, Parmesan-loaded meatballs ] bake them once and use them three completely different ways through the week.",
        "base": {
            "title": "Herby Parmesan Meatballs",
            "ingredients": [
                "2 lbs ground beef (80/20)",
                "1 cup grated Parmesan",
                "1 small yellow onion, grated",
                "6 cloves garlic, finely chopped",
                "1/2 cup breadcrumbs",
                "2 large eggs",
                "1/4 cup fresh parsley, minced",
                "2 tsp fresh rosemary, finely chopped",
                "1 tsp dried oregano",
                "1/2 tsp Aleppo pepper or red pepper flakes",
                "2 tsp salt",
            ],
            "steps": [
                "Preheat oven to 400 F. Line a baking sheet with parchment.",
                "Combine all ingredients in a large bowl, mixing gently [ do not overwork the meat.",
                "Use a cookie scoop to portion, then roll into balls and arrange on the baking sheet.",
                "Bake 15-20 minutes until the internal temperature reaches 165 F. Makes about 36 meatballs.",
            ],
        },
        "uses": [
            {
                "name": "Pearl Couscous Halloumi Bowl",
                "subtitle": "with arugula, pickled onions, and hot honey",
                "extras": [
                    "1 cup pearl couscous",
                    "Large handful arugula",
                    "1/3 cup pickled red onions",
                    "8 oz halloumi, sliced 1/2 inch thick",
                    "Juice of 1-2 lemons",
                    "Hot honey",
                    "Olive oil",
                ],
                "steps": [
                    "Cook pearl couscous according to package directions.",
                    "Pan-fry halloumi in olive oil over medium heat 2-3 minutes per side, drizzling hot honey as it cooks, until golden.",
                    "Toss couscous with arugula, pickled onions, olive oil, and lemon juice.",
                    "Divide between bowls and top each with 4-5 meatballs and the golden halloumi.",
                ],
                "tip": "Reheat meatballs at 50% microwave power, or in a 300 F oven covered with foil for 15 minutes.",
            },
            {
                "name": "Vodka Sauce Meatballs",
                "subtitle": "with pasta and Parmesan",
                "extras": [
                    "8 oz pasta of your choice",
                    "1 jar vodka sauce (Rao's works great)",
                    "Extra Parmesan and olive oil, to finish",
                ],
                "steps": [
                    "Cook pasta in salted boiling water until al dente.",
                    "Warm meatballs in vodka sauce in a pan over medium-low heat, stirring gently until heated through.",
                    "Toss pasta with the sauced meatballs.",
                    "Serve topped with extra Parmesan and a drizzle of olive oil.",
                ],
                "tip": None,
            },
            {
                "name": "Pesto Meatball Soup",
                "subtitle": "with pearl couscous and Parmesan",
                "extras": [
                    "4 cups chicken broth",
                    "2 cups cooked pearl couscous",
                    "2-3 tbsp pesto",
                    "Parmesan, for topping",
                ],
                "steps": [
                    "Add meatballs and chicken broth to a medium pot and bring to a gentle boil.",
                    "Stir in the cooked pearl couscous and warm through.",
                    "Serve with big dollops of pesto and freshly grated Parmesan.",
                ],
                "tip": "Add sauteed zucchini or a handful of spinach to bulk it out.",
            },
        ],
    },
    {
        "_id": "berry-parfait",
        "_keywords": ["parfait", "yogurt", "granola", "berries", "breakfast"],
        "image": "/static/images/parfait.jpg",
        "intro": "Homemade cinnamon granola layered with yogurt and fresh berries in a jar ] prep four on Sunday, keep the granola on the side, and add it right before eating so it stays crunchy all week.",
        "base": {
            "title": "Greek Yogurt Parfait",
            "ingredients": [
                "[ Granola ]",
                "1 cup old fashioned oats",
                "1 tsp cinnamon",
                "1/4 tsp salt",
                "3 tbsp butter",
                "3 tbsp brown sugar",
                "1/2 cup coconut flakes (optional)",
                "[ Assembly ]",
                "3–4 single-serve yogurt containers (plain, vanilla, or Greek)",
                "3 cups fresh berries of choice",
                "Honey, for drizzling (optional)",
            ],
            "steps": [
                "Preheat oven to 325°F.",
                "Melt butter and stir in brown sugar. In a bowl, combine oats, cinnamon, salt, and coconut flakes. Pour butter mixture over oats and stir well.",
                "Spread on a baking sheet and bake 20–25 minutes, stirring halfway, until golden brown. Cool completely on the pan.",
                "Layer yogurt and berries into jars or containers. Seal and refrigerate up to 4 days.",
                "Store granola in a separate airtight container at room temperature [ add it right before eating to keep it crunchy.",
            ],
        },
        "uses": [
            {
                "name": "Mixed Berry",
                "image": "",
                "subtitle": "strawberries, blueberries, and raspberries",
                "extras": [
                    "1 cup strawberries, sliced",
                    "1 cup blueberries",
                    "1 cup raspberries",
                    "Honey drizzle",
                ],
                "steps": [
                    "Layer yogurt, then a mix of all three berries, then another layer of yogurt in a jar.",
                    "Top with granola and a drizzle of honey just before eating.",
                ],
                "tip": "Add granola at the last minute ] it softens within an hour if layered in advance.",
            },
            {
                "name": "Strawberry",
                "image": "",
                "subtitle": "all strawberries with a hint of vanilla",
                "extras": [
                    "1½ cups strawberries, sliced",
                    "Vanilla yogurt (swap plain)",
                    "Honey drizzle",
                ],
                "steps": [
                    "Layer vanilla yogurt and sliced strawberries into a jar.",
                    "Top with granola and a drizzle of honey right before eating.",
                ],
                "tip": "Macerating the strawberries with a pinch of sugar for 10 minutes makes them extra juicy.",
            },
            {
                "name": "Blackberry and Blueberry",
                "image": "",
                "subtitle": "dark berries with coconut yogurt",
                "extras": [
                    "1 cup blackberries",
                    "1 cup blueberries",
                    "Coconut or plain yogurt",
                ],
                "steps": [
                    "Layer yogurt and berries into a jar, alternating layers.",
                    "Top with granola (and toasted coconut flakes if you made them) just before eating.",
                ],
                "tip": "Coconut yogurt pairs especially well with dark berries [ it adds a creamy, tropical contrast.",
            },
        ],
    },
    {
        "_id": "overnight-oats",
        "_keywords": ["oats"],
        "image": "/static/images/overnight-oats.jpg",
        "intro": "Five minutes of prep the night before means breakfast is already waiting ] make a batch on Sunday and vary the toppings all week.",
        "base": {
            "title": "Overnight Oats",
            "ingredients": [
                "1/2 cup whole rolled oats per serving",
                "1 tbsp chia seeds",
                "2/3 cup unsweetened almond milk (or milk of choice)",
                "1/4 cup whole milk Greek yogurt",
                "1/2 tsp maple syrup, plus more to taste",
                "Pinch of sea salt",
            ],
            "steps": [
                "In a jar or lidded container, combine oats, chia seeds, maple syrup, salt, and Greek yogurt.",
                "Pour in the milk and stir well to break up any chia seed clumps.",
                "Cover and refrigerate overnight, or up to 5 days.",
                "In the morning, stir again, add toppings of your choice, and eat cold or briefly warmed.",
            ],
        },
        "uses": [
            {
                "name": "Apple Pie",
                "subtitle": "with cinnamon, diced apple, and pecans",
                "extras": [
                    "2 tbsp unsweetened applesauce (stir into base before refrigerating)",
                    "1/4 tsp ground cinnamon (stir into base before refrigerating)",
                    "1/2 apple, diced",
                    "Handful of chopped pecans",
                    "Drizzle of maple syrup",
                ],
                "steps": [
                    "Before refrigerating, stir the applesauce and cinnamon into the oat base.",
                    "In the morning, top with diced apple, pecans, and a drizzle of maple syrup.",
                ],
                "tip": None,
            },
            {
                "name": "PB&J",
                "subtitle": "with chia jam, peanut butter, and fresh berries",
                "extras": [
                    "1-2 tbsp peanut butter",
                    "Fresh strawberries and raspberries, sliced",
                    "Chopped peanuts",
                    "Optional: chia jam (blend 1 cup berries with 1 tbsp chia seeds, refrigerate overnight)",
                ],
                "steps": [
                    "In the morning, spoon peanut butter over the cold oats.",
                    "Top with sliced berries, a dollop of chia jam, and chopped peanuts.",
                ],
                "tip": "Chia jam takes 5 minutes to make and keeps a week in the fridge.",
            },
            {
                "name": "Chocolate Banana",
                "subtitle": "with cocoa, banana, walnuts, and dark chocolate chips",
                "extras": [
                    "1/2 banana, mashed (stir into base before refrigerating)",
                    "1 tsp cocoa powder (stir into base before refrigerating)",
                    "1/4 tsp cinnamon and a pinch of nutmeg",
                    "1/2 banana, sliced (for topping)",
                    "Handful of walnuts",
                    "Small handful of dark chocolate chips",
                ],
                "steps": [
                    "Before refrigerating, stir mashed banana, cocoa powder, cinnamon, and nutmeg into the oat base.",
                    "In the morning, top with banana slices, walnuts, and chocolate chips.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "kale-salad-base",
        "_keywords": ["kale"],
        "image": "/static/images/kale-salad.jpg",
        "intro": "Massaged kale stays fresh and hearty all week [ one big batch of greens and carrot ginger dressing turns into a wrap, a grain bowl, and a warm pasta.",
        "base": {
            "title": "Massaged Kale with Carrot Ginger Dressing",
            "ingredients": [
                "1 large bunch curly kale, stems removed, torn into pieces",
                "1 tsp fresh lemon juice",
                "1/2 tsp olive oil",
                "1 can chickpeas, drained and roasted at 400 F for 20-25 min",
                "1 small carrot, grated",
                "1 small red beet, grated (optional)",
                "For dressing: 3/4 cup chopped carrots",
                "1/3 cup water",
                "1/4 cup olive oil",
                "2 tbsp rice vinegar",
                "2 tsp minced fresh ginger",
                "1/4 tsp sea salt",
            ],
            "steps": [
                "Make the dressing: roast carrot chunks at 400 F for 20-25 minutes until tender. Blend with water, olive oil, rice vinegar, ginger, and salt until smooth. Refrigerate.",
                "Place kale in a large bowl. Drizzle with lemon juice and olive oil and massage firmly for 2-3 minutes until the leaves soften and turn dark green.",
                "Toss in the grated carrot and beet if using. Store undressed in the fridge ] it holds up to 4 days.",
                "Roast chickpeas on a separate sheet at 400 F for 20-25 minutes until crispy. Season with salt and pepper.",
            ],
        },
        "uses": [
            {
                "name": "Kale Grain Bowl",
                "subtitle": "with quinoa, avocado, chickpeas, and carrot ginger dressing",
                "extras": [
                    "1 cup quinoa or farro, cooked",
                    "1 avocado, cubed",
                    "2 tbsp dried cranberries",
                    "1/4 cup pepitas, toasted",
                    "1 tsp sesame seeds",
                    "Watermelon radish or regular radish, thinly sliced (optional)",
                ],
                "steps": [
                    "Divide cooked quinoa or farro between bowls.",
                    "Pile on a generous handful of massaged kale.",
                    "Add avocado, grated carrot-beet mixture, cranberries, and pepitas.",
                    "Top with roasted chickpeas, sesame seeds, and a good pour of carrot ginger dressing.",
                ],
                "tip": "Add crumbled feta or Parmesan for extra richness.",
            },
            {
                "name": "Kale Wrap",
                "subtitle": "with chickpeas, avocado, and carrot ginger dressing",
                "extras": [
                    "Large flour or whole wheat tortillas",
                    "1 avocado, sliced",
                    "Optional: feta or hummus",
                ],
                "steps": [
                    "Warm tortillas briefly in a dry skillet.",
                    "Spread a thin layer of hummus or crumble feta down the center if using.",
                    "Add a handful of massaged kale, avocado slices, and a spoonful of the grated carrot mixture.",
                    "Add roasted chickpeas. Drizzle carrot ginger dressing over everything.",
                    "Fold and roll tightly. Slice in half and serve.",
                ],
                "tip": "Pack the dressing on the side if making these ahead [ it keeps the wrap from getting soggy.",
            },
            {
                "name": "Kale and Chickpea Pasta",
                "subtitle": "with lemon, Parmesan, and olive oil",
                "extras": [
                    "8 oz pasta (rigatoni or spaghetti work well)",
                    "3 tbsp olive oil",
                    "3 garlic cloves, thinly sliced",
                    "Juice of 1 lemon",
                    "1/2 cup grated Parmesan",
                    "Red pepper flakes",
                ],
                "steps": [
                    "Cook pasta in well-salted boiling water until al dente. Reserve 1/2 cup pasta water before draining.",
                    "Heat olive oil in a large pan over medium heat. Add garlic and a pinch of red pepper flakes, cook 1 minute until fragrant.",
                    "Add the massaged kale and roasted chickpeas to the pan and toss for 2 minutes.",
                    "Add the drained pasta and a splash of pasta water. Toss to coat.",
                    "Finish with lemon juice and Parmesan. Taste for salt and pepper.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "walder-shrimp",
        "_keywords": ["shrimp"],
        "image": "/static/images/Garlic-Shrimp.jpg",
        "intro": "A 15-minute shrimp base that pairs with three completely different vegetable sides ] fast enough for any weeknight, varied enough for the whole week.",
        "base": {
            "title": "Garlic Shrimp with Smoked Paprika & Honey",
            "ingredients": [
                "36 large shrimp, thawed, peeled and deveined",
                "3 tbsp olive oil, divided",
                "2 large cloves garlic, thinly sliced",
                "1.5 tsp smoked paprika",
                "1 tsp honey",
                "Salt and pepper",
            ],
            "steps": [
                "Pat shrimp dry with paper towels and toss with 1 tbsp olive oil, salt, and pepper.",
                "Heat a pan over medium-high until very hot [ a drop of water should sizzle immediately.",
                "Cook shrimp in a single layer 1-2 minutes per side until opaque and slightly browned. Work in batches if needed. Remove and set aside.",
                "In the same pan, add remaining olive oil and garlic. Saute 1 minute until fragrant and golden.",
                "Stir in smoked paprika and honey. Return shrimp and toss to coat. Remove from heat.",
            ],
        },
        "uses": [
            {
                "name": "Paprika Shrimp Bowl",
                "subtitle": "crispy chili potatoes, smoked paprika shrimp, and miso tahini",
                "extras": [
                    "2 lbs mini potatoes, quartered",
                    "1 tbsp chili powder, 1 tsp paprika, 1 tsp garlic powder",
                    "For miso tahini: 1/4 cup tahini, 2 tbsp miso paste, 6 tbsp nutritional yeast, 1/4 tsp garlic powder, 6 tbsp hot water",
                    "Fresh parsley or cilantro, to finish",
                ],
                "steps": [
                    "Toss quartered potatoes with olive oil, chili powder, paprika, garlic powder, salt, and pepper.",
                    "Roast at 400 F for 25-30 minutes, stirring halfway, until golden and crispy.",
                    "Blend miso tahini: combine tahini, miso, nutritional yeast, garlic powder, and hot water until smooth.",
                    "Pile crispy potatoes into a bowl, top with shrimp, and drizzle miso tahini generously over everything.",
                ],
                "tip": "Reheat potatoes in the oven or a hot pan to keep them crispy.",
            },
            {
                "name": "Honey Mustard Shrimp Bowl",
                "subtitle": "quinoa, honey-roasted broccolini and kale, with a tangy sauce swap",
                "extras": [
                    "1/2 cup dry quinoa",
                    "For honey mustard: 3 tbsp Dijon, 3 tbsp honey, 2 tsp white wine vinegar, 1/4 tsp cayenne",
                    "2 bunches broccolini, ends trimmed",
                    "4 cups curly kale, torn",
                    "2 tsp honey (for the broccolini)",
                ],
                "steps": [
                    "Cook quinoa according to package directions. Season with salt and a drizzle of olive oil.",
                    "Toss broccolini with olive oil, salt, and pepper. Roast at 350 F for 15 minutes. Add kale, drizzle with olive oil, roast 5 more minutes, then finish with honey.",
                    "Whisk together Dijon, honey, vinegar, and cayenne for the sauce.",
                    "Cook shrimp as in the base recipe, but toss with honey mustard sauce instead of paprika-honey.",
                    "Serve shrimp and broccolini over quinoa.",
                ],
                "tip": None,
            },
            {
                "name": "Green Bean Salad Plate",
                "subtitle": "garlic green beans, toasted almonds, and Dijon vinaigrette",
                "extras": [
                    "1-1.5 lbs green beans, ends trimmed",
                    "2 cloves garlic, minced",
                    "1/2 cup almonds, chopped",
                    "1/4 cup fresh parsley, chopped",
                    "For vinaigrette: 1/4 cup olive oil, 1 tbsp white wine vinegar, 3 tsp Dijon, 1/2 tsp garlic powder, 1 tbsp lemon juice",
                ],
                "steps": [
                    "Shake vinaigrette ingredients together in a jar.",
                    "Saute green beans with olive oil and garlic over medium heat for 10-12 minutes until tender-crisp.",
                    "Toast almonds in a dry pan 7-10 minutes until golden.",
                    "Toss green beans with vinaigrette, almonds, and parsley. Lay shrimp on top and serve with lemon wedges.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "walder-tofu",
        "_keywords": ["tofu"],
        "image": "/static/images/Baked-Tofu-Maple-Miso.jpg",
        "intro": "Crispy miso-maple tofu is the weeknight protein that makes vegetables the main event ] three bold pairings, all built around the same golden base.",
        "base": {
            "title": "Baked Miso Maple Tofu",
            "ingredients": [
                "1 block (14-16 oz) firm tofu",
                "1 tbsp avocado oil",
                "1 tbsp cornstarch",
                "For sauce: 1/4 cup mirin, 1 tbsp miso paste, 1 tbsp rice vinegar, 1 tbsp avocado oil, 1 tbsp maple syrup",
            ],
            "steps": [
                "Preheat oven to 400 F.",
                "Cut tofu into 1-inch cubes. Press gently with paper towels to remove excess moisture.",
                "Toss tofu with avocado oil, then add cornstarch and toss again until evenly coated.",
                "Spread on a baking sheet and bake 20 minutes, flipping halfway, until golden and crispy.",
                "Make the sauce: bring mirin to a boil in a small saucepan for 1 minute. Reduce heat and whisk in miso until dissolved. Add rice vinegar, oil, and maple syrup. Bring back to a brief boil, whisking continuously, then remove from heat.",
                "Pour sauce over baked tofu and toss to coat.",
            ],
        },
        "uses": [
            {
                "name": "Butternut Squash Bowl",
                "subtitle": "roasted squash and kale with spicy coconut cream",
                "extras": [
                    "1 medium butternut squash, peeled and cut into 3/4-inch cubes",
                    "3 cloves garlic, minced",
                    "4 cups curly kale, torn",
                    "1/2 cup almonds, chopped",
                    "For coconut sauce: 1 cup canned coconut cream, 1 tsp red pepper flakes, 1 tsp cornstarch",
                ],
                "steps": [
                    "Toss squash with olive oil, garlic, and salt. Roast at 400 F for 30 minutes, stirring halfway.",
                    "Stir in kale and almonds, return to oven for 6 more minutes until kale is slightly crispy.",
                    "Simmer coconut cream, whisk in red pepper flakes and cornstarch, cook 10-15 minutes until thickened.",
                    "Bowl up the squash and kale. Drizzle coconut sauce over the top and add miso maple tofu.",
                ],
                "tip": None,
            },
            {
                "name": "Curried Cauliflower Bowl",
                "subtitle": "with rice, cashews, raisins, and minty yogurt",
                "extras": [
                    "1 cup dry white rice",
                    "2 heads cauliflower, cut into 1-inch florets",
                    "3 tbsp olive oil, 1.5 tsp curry powder, 1 tsp garlic powder",
                    "1/4 cup cashews and 3 tbsp raisins",
                    "For minty yogurt: 3/4 cup Greek yogurt, 1 cup fresh mint, 3 tbsp lemon juice, 1 tsp garlic powder",
                ],
                "steps": [
                    "Cook rice according to package directions.",
                    "Toss cauliflower with olive oil, curry powder, garlic powder, and salt. Roast at 425 F for 25 minutes until browned.",
                    "Blend yogurt, mint, lemon juice, and garlic powder until smooth. Season with salt.",
                    "Serve rice topped with miso tofu and curry cauliflower. Scatter cashews and raisins over the top and drizzle minty yogurt generously.",
                ],
                "tip": "Make extra minty yogurt [ it keeps 3-4 days and is great on everything.",
            },
            {
                "name": "Miso Zucchini Pasta",
                "subtitle": "golden parmesan zucchini tossed with pasta and miso tahini",
                "extras": [
                    "8 oz pasta (rigatoni or fusilli)",
                    "2 large zucchini, cut into 3-inch strips",
                    "1/2 tsp garlic powder and 1/2 tsp dried basil",
                    "1/4 cup Parmigiano-Reggiano, freshly grated",
                    "For miso tahini: 1/4 cup tahini, 2 tbsp miso paste, 6 tbsp nutritional yeast, 1/4 tsp garlic powder, 6 tbsp hot water",
                    "Fresh basil, to finish",
                ],
                "steps": [
                    "Toss zucchini with olive oil, garlic powder, dried basil, salt, and Parmesan. Spread on parchment.",
                    "Roast at 400 F for 20 minutes until fork-tender and cheese is golden.",
                    "Cook pasta in salted boiling water until al dente. Reserve 1/4 cup pasta water before draining.",
                    "Blend miso tahini: combine tahini, miso, nutritional yeast, garlic powder, and hot water until smooth. Thin with pasta water if needed.",
                    "Toss pasta with roasted zucchini, miso tofu, and miso tahini sauce. Finish with fresh basil.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "hot-honey-zaatar-turkey",
        "_keywords": ["turkey", "za"],
        "image": "/static/images/zataar-sausage.png",
        "intro": "A Middle Eastern-spiced turkey sausage with hot honey and zaatar ] cook it once and it turns into a grain bowl, a creamy pasta, and a breakfast frittata.",
        "base": {
            "title": "Hot Honey Zaatar Turkey Sausage",
            "ingredients": [
                "2 lbs ground turkey (93% lean)",
                "2 tbsp avocado oil",
                "2 tbsp butter",
                "1/4 cup + 1 tbsp hot honey",
                "2 tbsp zaatar",
                "2 tsp onion powder",
                "1 tsp garlic powder",
                "1 tsp smoked paprika",
                "1 1/2 tsp salt",
                "Black pepper to taste",
            ],
            "steps": [
                "Heat avocado oil in a large skillet over medium-high heat.",
                "Add ground turkey in large chunks and cook undisturbed for 5 minutes to develop some browning.",
                "Break into smaller pieces, then add butter, hot honey, and all the seasonings.",
                "Cook 5-6 more minutes, stirring occasionally, until turkey reaches 165 F and is lightly browned throughout.",
            ],
        },
        "uses": [
            {
                "name": "Mediterranean Grain Bowl",
                "subtitle": "with quinoa, labneh, and Greek salad",
                "image": "",
                "extras": [
                    "1 cup quinoa",
                    "2 cups arugula",
                    "1 roma tomato, diced",
                    "1 Persian cucumber, diced",
                    "1/4 red onion, sliced",
                    "1/3 cup crumbled feta",
                    "1/2 cup jarred roasted red peppers, sliced",
                    "2 tbsp labneh or Greek yogurt per bowl",
                    "Juice of 1 1/2 lemons, 1 tsp Dijon, 1 tbsp red wine vinegar, 2 tbsp olive oil, 1/4 tsp hot honey (for dressing)",
                ],
                "steps": [
                    "Cook quinoa according to package directions.",
                    "Whisk together lemon juice, Dijon, red wine vinegar, olive oil, hot honey, salt, and pepper for the dressing.",
                    "Toss tomatoes, cucumber, red onion, and feta together for the Greek salad component.",
                    "Divide quinoa and arugula between bowls. Add turkey, Greek salad, roasted red peppers, and a spoonful of labneh.",
                    "Pour dressing over everything. Garnish with fresh dill or pickled onions.",
                ],
                "tip": None,
            },
            {
                "name": "Zucchini Pasta",
                "subtitle": "with Parmesan, basil, and turkey sausage",
                "image": "",
                "extras": [
                    "8 oz pasta",
                    "1 1/2 zucchini, thinly sliced",
                    "1 shallot, finely diced",
                    "4 cloves garlic, chopped",
                    "3 cups vegetable broth",
                    "1 cup grated Parmesan",
                    "Juice of 1 lemon",
                    "1/4 cup fresh basil, sliced",
                    "Olive oil",
                ],
                "steps": [
                    "Heat olive oil in a large skillet. Spread zucchini in a single layer, season with salt, and cook over medium heat for 10 minutes until golden. Transfer to a plate.",
                    "In the same pan, saute shallots and garlic over medium-low for 2-3 minutes.",
                    "Add turkey sausage, dry pasta, and broth. Bring to a boil, then reduce heat, cover, and simmer 10-15 minutes until pasta is al dente.",
                    "Turn heat to low. Stir in zucchini, Parmesan, basil, and lemon juice until the sauce is creamy.",
                    "Serve with extra basil and Parmesan.",
                ],
                "tip": "Check pasta at 10 minutes [ cooking time varies by shape.",
            },
            {
                "name": "Potato and Goat Cheese Frittata",
                "subtitle": "with kale, gold potatoes, and turkey sausage",
                "image": "",
                "extras": [
                    "10 eggs",
                    "1 cup thinly sliced small gold potatoes",
                    "2 cups kale, roughly chopped",
                    "1/2 yellow onion, sliced",
                    "4 oz goat cheese",
                    "4 cloves garlic",
                    "Olive oil",
                ],
                "steps": [
                    "Preheat oven to 350 F.",
                    "In an oven-safe skillet, saute kale and garlic in olive oil over medium-low, covered, for 2-3 minutes until wilted. Remove and set aside.",
                    "Add more oil, then cook potatoes and onion, covered, over medium heat for 7-9 minutes until soft.",
                    "Beat eggs with salt and pepper. Add kale back to the pan, then pour eggs over everything. Cook 2-3 minutes until edges begin to set.",
                    "Crumble goat cheese on top. Transfer to oven and bake 10 minutes until fully set.",
                ],
                "tip": "Reheat leftovers in the microwave or in a 400 F oven for 10 minutes.",
            },
        ],
    },
    {
        "_id": "bean-veggie-salad",
        "_keywords": ["beans", "vegetarian", "vegan", "salad", "lunch"],
        "image": "/static/images/BEAN-SALAD.jpg",
        "intro": "One lemony, herb-packed bowl of marinated cannellini beans and vegetables that you make once and eat five different ways ] on toast, in a pita, or tossed with greens.",
        "base": {
            "title": "Easy Lemon-Marinated Bean and Vegetable Salad",
            "ingredients": [
                "1 clove garlic, finely chopped",
                "Zest and juice of 1 medium lemon (about 1/4 cup juice)",
                "2 tbsp olive oil",
                "1 tsp kosher salt",
                "1/2 tsp honey",
                "3 stalks celery, halved lengthwise and thinly sliced (about 2 cups)",
                "1 small red onion, halved and thinly sliced (about 1 cup)",
                "2 cups grape or cherry tomatoes, halved",
                "1/2 cup fresh parsley leaves, coarsely chopped",
                "2 tbsp fresh dill fronds, coarsely chopped",
                "3 (15-oz) cans cannellini beans or chickpeas, drained and rinsed",
                "1 1/2 oz Pecorino Romano or Parmesan, finely grated (about 1/3 cup), plus more for serving",
                "Freshly ground black pepper, for serving",
            ],
            "steps": [
                "In a large bowl, whisk together garlic, lemon zest, lemon juice, olive oil, salt, and honey.",
                "Add celery, red onion, tomatoes, parsley, dill, cannellini beans, and Pecorino Romano to the bowl.",
                "Toss gently to combine. Taste and adjust seasoning.",
                "Serve with more cheese and black pepper. Keeps refrigerated up to 5 days.",
            ],
        },
        "uses": [
            {
                "name": "Pita Pocket",
                "subtitle": "with hummus or avocado",
                "extras": ["Pita bread", "2 tbsp hummus or 1/2 avocado, sliced"],
                "steps": [
                    "Swipe the inside of a pita with hummus or avocado.",
                    "Stuff generously with bean salad.",
                ],
                "tip": None,
            },
            {
                "name": "Egg on Top",
                "subtitle": "with an over-easy egg",
                "extras": ["1 egg", "1 tsp butter or olive oil"],
                "steps": [
                    "Heat butter in a skillet over medium heat. Crack in the egg and cook until whites are set but yolk is still runny.",
                    "Spoon bean salad into a bowl and slide the egg on top. Let the jammy yolk drape over the beans.",
                ],
                "tip": None,
            },
            {
                "name": "Grain Bowl",
                "subtitle": "with reheated farro, quinoa, or rice",
                "extras": ["1 cup cooked grains (quinoa, farro, or rice)"],
                "steps": [
                    "Reheat leftover cooked grains.",
                    "Serve bean salad alongside a scoop of warm grains.",
                ],
                "tip": "Beans and grains are perfect protein partners.",
            },
            {
                "name": "Greens Bowl",
                "subtitle": "tossed with arugula or baby spinach",
                "extras": ["2 cups arugula or baby spinach"],
                "steps": [
                    "Add arugula or spinach to the bean salad and toss [ no extra dressing needed.",
                ],
                "tip": None,
            },
            {
                "name": "Smashed Bean Toast",
                "subtitle": "on crusty bread",
                "extras": ["1 thick slice crusty bread, toasted"],
                "steps": [
                    "Coarsely smash some of the beans with a fork.",
                    "Pile onto toasted bread and finish with more black pepper and cheese.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "protein-overnight-oats",
        "_keywords": ["oats", "breakfast", "protein"],
        "image": "/static/images/High-Protein-Overnight-Oats.jpg",
        "intro": "40 grams of protein and 10 grams of fiber in a jar you prep the night before ] make four on Sunday and breakfast is handled all week.",
        "base": {
            "title": "High Protein Overnight Oats",
            "ingredients": [
                "1/3 cup old-fashioned oats",
                "1/4 cup nonfat plain or vanilla Greek yogurt",
                "1/3 cup milk of choice",
                "1 tbsp chia seeds",
                "1 tbsp ground flax seeds",
                "1 scoop protein powder",
            ],
            "steps": [
                "Add all ingredients to a wide-mouth pint-sized mason jar or container.",
                "Seal and stir or shake until evenly combined.",
                "Let sit 5 minutes, then stir again to prevent oats and seeds from settling.",
                "Refrigerate overnight or at least 4 hours.",
                "In the morning, stir, add toppings, and enjoy cold.",
            ],
        },
        "uses": [
            {
                "name": "Chocolate Raspberry",
                "subtitle": "with chocolate protein powder, cocoa, and fresh raspberries",
                "extras": [
                    "1 scoop chocolate protein powder (swap in for plain)",
                    "1 tbsp Dutch-processed cocoa powder",
                    "1/2 cup fresh raspberries",
                    "1 tsp honey or maple syrup (optional)",
                ],
                "steps": [
                    "Make the base using chocolate protein powder and stir in cocoa powder before refrigerating.",
                    "In the morning, top with fresh raspberries and a drizzle of honey.",
                ],
                "tip": None,
            },
            {
                "name": "PB Banana",
                "subtitle": "with peanut butter drizzle and banana slices",
                "extras": ["1 tbsp peanut butter", "1/2 banana, sliced", "Handful of peanuts"],
                "steps": ["In the morning, top oats with peanut butter, banana slices, and peanuts."],
                "tip": None,
            },
            {
                "name": "Strawberry",
                "subtitle": "with fresh strawberries and honey",
                "extras": ["1/2 cup fresh strawberries, sliced", "1 tsp honey"],
                "steps": ["In the morning, top oats with sliced strawberries and a drizzle of honey."],
                "tip": None,
            },
        ],
    },
    {
        "_id": "sausage-egg-bites",
        "_keywords": ["eggs", "breakfast", "protein", "sausage"],
        "image": "/static/images/Sausage-Egg-Bites.jpg",
        "intro": "12 protein-packed egg bites you bake on Sunday and microwave in 30 seconds all week [ sausage, cottage cheese, and vegetables baked into creamy, portable bites.",
        "base": {
            "title": "Sausage Egg Bites",
            "ingredients": [
                "9 eggs",
                "1 cup low-fat cottage cheese",
                "1/2 tsp Italian seasoning",
                "1/4 tsp garlic powder",
                "1/4 tsp onion powder",
                "1/4 tsp smoked paprika",
                "Salt and pepper",
                "1/2 cup cooked chicken breakfast sausage, chopped",
                "1/2 cup fresh spinach, chopped",
                "1/2 cup bell pepper, finely chopped",
                "1/4 cup grated Parmesan",
            ],
            "steps": [
                "Preheat oven to 350 F. Grease a 12-cup muffin tin.",
                "In a blender, combine eggs, cottage cheese, Italian seasoning, garlic powder, onion powder, paprika, salt, and pepper. Blend 1 minute until smooth.",
                "Pour egg mixture evenly into muffin tin. Distribute sausage, spinach, and bell pepper among the cups. Gently push ingredients down.",
                "Top each cup with grated Parmesan.",
                "Place muffin tin on a rimmed baking sheet. Pour 1-2 cups water onto the sheet pan to create a water bath.",
                "Bake 25-30 minutes until eggs are set and not jiggling. Cool briefly before removing.",
            ],
        },
        "uses": [
            {
                "name": "Grab-and-Go",
                "subtitle": "30 seconds in the microwave",
                "extras": [],
                "steps": [
                    "Store egg bites in an airtight container in the fridge up to 4 days.",
                    "Microwave 30-60 seconds until warmed through.",
                ],
                "tip": "Freeze up to 30 days ] thaw overnight or microwave from frozen for 60-90 seconds.",
            },
            {
                "name": "Veggie Swap",
                "subtitle": "with kale, mushrooms, or zucchini",
                "extras": ["1/2 cup kale, mushrooms, or zucchini, finely chopped (swap for spinach or peppers)"],
                "steps": [
                    "Swap spinach or bell pepper for any finely chopped vegetable.",
                    "Follow the base recipe steps.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "breakfast-burritos",
        "_keywords": ["eggs", "breakfast", "protein", "burrito", "turkey"],
        "image": "/static/images/High-Protein-Breakfast-Burritos.jpg",
        "intro": "Over 30 grams of protein per burrito [ scrambled eggs, turkey sausage, peppers, onions, and pepper jack wrapped in a low-carb tortilla. Make a batch on Sunday and toast one each morning.",
        "base": {
            "title": "High Protein Breakfast Burritos",
            "ingredients": [
                "6 large eggs",
                "1 cup egg whites",
                "12 oz turkey breakfast sausage",
                "1 red bell pepper, diced",
                "1 yellow onion, diced",
                "1/2 tsp garlic powder",
                "Salt and pepper to taste",
                "6 large low-carb tortillas",
                "6 oz shredded pepper jack cheese",
            ],
            "steps": [
                "Whisk eggs and egg whites together. Pour into a nonstick skillet over medium heat and scramble 3-5 minutes until just cooked. Transfer to a plate and refrigerate to cool.",
                "In the same skillet over medium-high, cook turkey sausage, breaking into pieces, 4-6 minutes until browned. Drain excess fat.",
                "Add peppers, onions, and garlic powder. Sauté 3-4 minutes until vegetables are tender. Transfer to a plate and refrigerate at least 30 minutes.",
                "Lay out tortillas. Layer each with cheese, cooled scrambled eggs, and the sausage-veggie mixture.",
                "Fold in the sides and roll up tightly. Toast in a skillet or wrap in foil for storage.",
            ],
        },
        "uses": [
            {
                "name": "Toasted Skillet",
                "subtitle": "golden and crispy on the outside",
                "extras": ["1 tsp butter or olive oil"],
                "steps": [
                    "Heat butter in a skillet over medium heat.",
                    "Place burrito seam-side down and cook 2-3 minutes per side until golden and crispy.",
                ],
                "tip": "Always cool fillings before assembling ] warm fillings make the tortilla soggy.",
            },
            {
                "name": "Freezer Batch",
                "subtitle": "wrapped and frozen for the month",
                "extras": ["Aluminum foil"],
                "steps": [
                    "Wrap each assembled burrito tightly in foil.",
                    "Freeze up to 3 months. Reheat from frozen in a 350 F oven for 25-30 minutes, or unwrap and microwave 2-3 minutes.",
                ],
                "tip": None,
            },
            {
                "name": "Loaded Veggie",
                "subtitle": "with mushrooms, spinach, or zucchini",
                "extras": ["1/2 cup mushrooms, spinach, or zucchini, diced"],
                "steps": [
                    "Add extra vegetables when sautéing the peppers and onions.",
                    "Follow the base recipe steps.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "bacon-gruyere-egg-bites",
        "_keywords": ["eggs", "breakfast", "protein", "bacon"],
        "image": "/static/images/Bacon-Gruyere-Egg-Bites.jpg",
        "intro": "A homemade take on Starbucks sous vide egg bites [ creamy, protein-packed, and made with just a muffin tin. Bacon, Gruyère, and cottage cheese baked into 12 portable bites.",
        "base": {
            "title": "Bacon Gruyère Egg Bites",
            "ingredients": [
                "9 large eggs",
                "1 cup cottage cheese or ricotta",
                "1/2 cup shredded Gruyère cheese",
                "1/4 tsp garlic powder",
                "1/4 tsp onion powder",
                "1/4 tsp kosher salt",
                "1/4 tsp black pepper",
                "1/2 cup cooked bacon, chopped",
            ],
            "steps": [
                "Preheat oven to 375 F. Spray a 12-cup muffin tin with nonstick spray. Set the tin in a rimmed baking sheet and fill the baking sheet halfway with water to create a water bath.",
                "Blend eggs, cottage cheese, Gruyère, and seasonings until smooth. Pour evenly into muffin cups.",
                "Top each cup with chopped bacon.",
                "Bake 35-40 minutes until centers are firm. Cool 5 minutes before removing ] they'll deflate slightly, which is normal.",
            ],
        },
        "uses": [
            {
                "name": "Grab-and-Go",
                "subtitle": "30-60 seconds in the microwave",
                "extras": [],
                "steps": [
                    "Store in an airtight container in the fridge up to 3 days.",
                    "Microwave 30-60 seconds until heated through.",
                ],
                "tip": "Freeze individually. Reheat from frozen at 40% microwave power for 1:30-3:00.",
            },
            {
                "name": "Veggie Swap",
                "subtitle": "with roasted peppers, spinach, or mushrooms",
                "extras": ["1/4 cup roasted peppers, spinach, or mushrooms, finely chopped"],
                "steps": [
                    "Add a spoonful of vegetables to each muffin cup after pouring in the egg mixture.",
                    "Follow the base recipe steps.",
                ],
                "tip": None,
            },
            {
                "name": "Meat Swap",
                "subtitle": "with Canadian bacon, turkey sausage, or ham",
                "extras": ["1/2 cup Canadian bacon, turkey sausage, or ham, chopped"],
                "steps": [
                    "Swap chopped bacon for any cooked, chopped protein.",
                    "Follow the base recipe steps.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "protein-chia-pudding",
        "_keywords": ["chia", "breakfast", "protein", "pudding"],
        "image": "/static/images/Protein-Chia-Seed-Pudding.jpg",
        "intro": "36 grams of protein from just 4 ingredients [ Greek yogurt, milk, protein powder, and chia seeds. Prep in 5 minutes on Sunday and eat all week with endless flavor variations.",
        "base": {
            "title": "Protein Chia Seed Pudding",
            "ingredients": [
                "1/2 cup nonfat plain Greek yogurt",
                "1/3 cup milk of choice",
                "1 scoop protein powder",
                "2 tbsp chia seeds",
            ],
            "steps": [
                "Add Greek yogurt, milk, and protein powder to a 10-oz glass container. Whisk until smooth.",
                "Add chia seeds and whisk until evenly distributed.",
                "Cover and refrigerate at least 2 hours until thick.",
                "Before serving, stir and add a splash of milk if you want a thinner consistency. Top as desired.",
            ],
        },
        "uses": [
            {
                "name": "Peanut Butter",
                "subtitle": "with vanilla protein and a peanut butter swirl",
                "extras": ["1 tbsp peanut butter", "1 extra tbsp milk", "Vanilla protein powder"],
                "steps": [
                    "Use vanilla protein powder. Whisk in peanut butter and extra milk with the base.",
                    "Refrigerate overnight. Top with a drizzle of peanut butter before serving.",
                ],
                "tip": None,
            },
            {
                "name": "Chocolate",
                "subtitle": "with cocoa powder",
                "extras": ["Chocolate protein powder", "2-3 tsp unsweetened cocoa powder", "Honey to taste"],
                "steps": [
                    "Use chocolate protein powder. Whisk in cocoa powder with the base.",
                    "Refrigerate overnight. Add honey if you want extra sweetness.",
                ],
                "tip": None,
            },
            {
                "name": "Strawberry",
                "subtitle": "with strawberry protein and fresh berries",
                "extras": ["Strawberry protein powder", "1/2 cup fresh strawberries, chopped"],
                "steps": [
                    "Use strawberry protein powder. Refrigerate overnight.",
                    "Top with fresh chopped strawberries before serving.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "cosmic-brownie-oats",
        "_keywords": ["oats", "breakfast", "protein", "chocolate"],
        "image": "/static/images/Cosmic-Brownie-Overnight-Oats.jpg",
        "intro": "A nostalgia-fueled dessert-for-breakfast jar ] fudgy brownie overnight oats topped with a layer of chocolate protein frosting and candy sprinkles. 44 grams of protein.",
        "base": {
            "title": "Cosmic Brownie Overnight Oats",
            "ingredients": [
                "1/4 cup old-fashioned oats",
                "1 scoop chocolate protein powder",
                "1 tbsp chia seeds",
                "1 tbsp ground flax seeds",
                "1 tbsp Dutch-processed cocoa powder",
                "1/3 cup milk of choice",
                "1/4 cup nonfat plain Greek yogurt",
                "For frosting: 2 tbsp Greek yogurt + 1/2 tbsp milk + 1 tbsp chocolate protein powder + 1 tsp cocoa powder",
                "1 tsp candy-coated chocolate sprinkles",
            ],
            "steps": [
                "Combine oats, chocolate protein powder, chia seeds, flax seeds, cocoa powder, milk, and Greek yogurt in a 10-oz jar. Whisk until fully blended.",
                "In a separate small bowl, whisk together frosting ingredients until smooth. Pour over the oat base and smooth into an even layer.",
                "Cover and refrigerate overnight or at least 4 hours.",
                "Top with candy sprinkles just before serving.",
            ],
        },
        "uses": [
            {
                "name": "Meal Prep Batch",
                "subtitle": "four jars for the week",
                "extras": [],
                "steps": [
                    "Multiply the recipe by 4. Prepare all jars on Sunday.",
                    "Refrigerate up to 4-5 days. Add sprinkles to each jar just before eating.",
                ],
                "tip": "Do not freeze [ the texture changes significantly.",
            },
        ],
    },
    {
        "_id": "cottage-cheese-bowl",
        "_keywords": ["cottage cheese", "breakfast", "protein"],
        "image": "/static/images/Cottage-Cheese-Bowl.jpg",
        "intro": "30 grams of protein, zero cooking. Spoon cottage cheese into a bowl, go sweet with berries, granola, and peanut butter ] or savory with avocado, tomatoes, and everything bagel seasoning.",
        "base": {
            "title": "Cottage Cheese Bowls",
            "ingredients": [
                "1 cup (2%) cottage cheese per bowl",
                "[ Sweet toppings ]",
                "1 cup mixed berries (strawberries, blueberries, blackberries)",
                "1/3 cup granola",
                "1 tbsp peanut butter or almond butter",
                "1 tsp honey or maple syrup",
                "Pinch of cinnamon",
                "[ Savory toppings ]",
                "1/2 cup cherry tomatoes, halved",
                "1/2 cup cucumber, diced",
                "1/3 cup avocado, diced",
                "1 tsp extra virgin olive oil",
                "1 tsp balsamic vinegar",
                "Everything bagel seasoning",
                "Fresh chives, chopped",
            ],
            "steps": [
                "Spoon 1 cup cottage cheese into a bowl.",
                "For sweet: arrange berries on top, add granola, dollop peanut butter, drizzle honey, and finish with a pinch of cinnamon.",
                "For savory: top with tomatoes, cucumber, and avocado. Drizzle with olive oil and balsamic, then sprinkle everything bagel seasoning and chives.",
                "Prep bowls ahead: store plain cottage cheese in containers up to 5 days. Add fresh toppings each morning.",
            ],
        },
        "uses": [
            {
                "name": "Sweet Berry Bowl",
                "image": "",
                "subtitle": "berries, granola, peanut butter, and honey",
                "extras": [
                    "1 cup mixed berries",
                    "1/3 cup granola",
                    "1 tbsp peanut butter",
                    "1 tsp honey",
                    "Pinch of cinnamon",
                ],
                "steps": [
                    "Spoon cottage cheese into a bowl.",
                    "Top with berries, granola, a dollop of peanut butter, a drizzle of honey, and a pinch of cinnamon.",
                ],
                "tip": "Swap peanut butter for almond butter or tahini, and use any seasonal fruit [ banana, mango, or peach all work great.",
            },
            {
                "name": "Savory Avocado Bowl",
                "image": "",
                "subtitle": "tomatoes, cucumber, avocado, and everything bagel seasoning",
                "extras": [
                    "1/2 cup cherry tomatoes, halved",
                    "1/2 cup cucumber, diced",
                    "1/3 cup avocado, diced",
                    "1 tsp olive oil",
                    "1 tsp balsamic vinegar",
                    "Everything bagel seasoning",
                    "Fresh chives",
                ],
                "steps": [
                    "Spoon cottage cheese into a bowl.",
                    "Arrange tomatoes, cucumber, and avocado on top.",
                    "Drizzle with olive oil and balsamic, then finish with everything bagel seasoning and chives.",
                ],
                "tip": "Add a hard-boiled egg on the side for extra protein. Bell pepper, radishes, or roasted beets are great additions too.",
            },
        ],
    },
    {
        "_id": "crustless-veggie-quiche",
        "_keywords": ["eggs", "breakfast", "quiche", "vegetables"],
        "image": "/static/images/crustless-quiche.jpg",
        "intro": "One pie dish baked on Sunday, sliced into a full week of breakfasts. Sautéed shallots, broccoli, and Gruyère ] elegant enough for brunch, easy enough for meal prep. Reheats in 90 seconds.",
        "base": {
            "title": "Crustless Veggie Quiche",
            "ingredients": [
                "6 large eggs",
                "1/2 cup milk (any kind [ whole, 2%, or almond)",
                "1/2 tsp sea salt, plus more to taste",
                "Freshly ground black pepper",
                "1 tbsp extra virgin olive oil, plus more for the dish",
                "2 shallots, thinly sliced",
                "3 cups small broccoli florets (about 6 oz)",
                "1/4 cup water",
                "1 cup grated Gruyère cheese",
                "1 tbsp fresh thyme leaves or chopped chives",
            ],
            "steps": [
                "Preheat oven to 350°F. Grease a 9-inch pie dish with olive oil.",
                "Whisk together eggs, milk, salt, and several grinds of pepper in a large bowl.",
                "Heat olive oil in a skillet over medium heat. Add shallots with a pinch of salt and cook 4–5 minutes until softened.",
                "Add broccoli and water to the skillet. Cook 4 minutes until the water evaporates and the broccoli is bright green.",
                "Transfer vegetables to the pie dish in an even layer. Sprinkle cheese over the top.",
                "Pour the egg mixture over the vegetables and gently shake the dish to distribute evenly. Sprinkle with thyme.",
                "Bake 30–40 minutes until the eggs are set and the edges are golden brown.",
                "Cool 10 minutes before slicing into wedges. Store in the fridge up to 3 days, or freeze up to 3 months.",
            ],
        },
        "uses": [
            {
                "name": "Broccoli and Gruyère",
                "image": "",
                "subtitle": "the classic ] shallots, broccoli, and melty Gruyère",
                "extras": [],
                "steps": [
                    "Follow the base recipe as written.",
                    "Serve warm or at room temperature with a simple green salad.",
                ],
                "tip": "Can be prepped the night before and baked the next morning straight from the fridge.",
            },
            {
                "name": "Spinach and Feta",
                "image": "",
                "subtitle": "wilted spinach with tangy crumbled feta",
                "extras": [
                    "3 cups fresh spinach (swap for broccoli)",
                    "3/4 cup crumbled feta (swap for Gruyère)",
                    "2 garlic cloves, minced (add with shallots)",
                ],
                "steps": [
                    "Sauté shallots and garlic in olive oil. Add spinach and cook 1–2 minutes until wilted.",
                    "Transfer to pie dish, top with feta, then pour egg mixture over. Sprinkle with thyme.",
                    "Bake 30–40 minutes until set.",
                ],
                "tip": "Squeeze excess moisture from the spinach before adding to the dish [ this keeps the quiche from getting watery.",
            },
            {
                "name": "Mushroom and Goat Cheese",
                "image": "",
                "subtitle": "earthy cremini mushrooms with tangy goat cheese",
                "extras": [
                    "8 oz cremini mushrooms, sliced (swap for broccoli)",
                    "3 oz goat cheese, crumbled (swap for Gruyère)",
                    "1 tsp fresh rosemary or thyme",
                ],
                "steps": [
                    "Sauté shallots and mushrooms until golden and most liquid has evaporated, about 6–8 minutes.",
                    "Transfer to pie dish, crumble goat cheese over top, then pour egg mixture over. Sprinkle with rosemary.",
                    "Bake 30–40 minutes until set.",
                ],
                "tip": "Don't rush the mushrooms ] letting them cook until golden (not steamed) gives the best flavor.",
            },
        ],
    },
    {
        "_id": "sheet-pan-eggs",
        "_keywords": ["eggs", "breakfast", "protein"],
        "image": "/static/images/Sheet-Pan-Eggs.jpg",
        "intro": "Blend, pour, bake [ one sheet pan of fluffy eggs sliced into squares becomes wraps, bowls, and sandwiches for the whole week. Faster than scrambling individual portions every morning.",
        "base": {
            "title": "Sheet Pan Eggs",
            "ingredients": [
                "18 large eggs",
                "1/3 cup milk",
                "1/2 tsp salt",
                "1/2 tsp black pepper",
                "1/2 cup diced red bell pepper",
                "1/2 cup shredded cheddar cheese",
                "Optional: up to 1 cup chopped spinach, mushrooms, or onion",
                "Cooked bacon, sausage, or ham (optional)",
                "Parchment paper or nonstick spray",
            ],
            "steps": [
                "Preheat oven to 350°F. Line a rimmed 12×17-inch sheet pan with parchment paper and grease well.",
                "Combine eggs, milk, salt, and pepper in a blender and blend until smooth ] this makes the eggs extra fluffy.",
                "Pour the egg mixture evenly onto the prepared sheet pan.",
                "Sprinkle with bell pepper, cheese, and any additional vegetables or protein.",
                "Bake 17–20 minutes until fully set with no liquid in the center.",
                "Cool 10 minutes before slicing into squares [ cooling helps them hold their shape.",
                "Store in the fridge up to 4 days, or freeze individual portions up to 2 months. Reheat 30–60 seconds in the microwave.",
            ],
        },
        "uses": [
            {
                "name": "Breakfast Wrap",
                "image": "",
                "subtitle": "in a tortilla with salsa, avocado, and hot sauce",
                "extras": [
                    "Large flour tortilla",
                    "2 tbsp salsa",
                    "1/4 avocado, sliced",
                    "Hot sauce, to taste",
                ],
                "steps": [
                    "Warm a tortilla in a dry pan or microwave for 20 seconds.",
                    "Place 1–2 egg squares in the center, top with salsa and avocado, and add hot sauce.",
                    "Fold in the sides and roll up tightly.",
                ],
                "tip": "Add leftover black beans or a sprinkle of cotija cheese for a Southwest twist.",
            },
            {
                "name": "Breakfast Bowl",
                "image": "",
                "subtitle": "over roasted potatoes or grains with toppings",
                "extras": [
                    "1 cup roasted potatoes, hash browns, or cooked grains",
                    "1/4 avocado, sliced",
                    "2 tbsp salsa or hot sauce",
                    "Sour cream or Greek yogurt (optional)",
                ],
                "steps": [
                    "Warm roasted potatoes or grains in the microwave.",
                    "Place 1–2 egg squares on top and add avocado and salsa.",
                    "Finish with a dollop of sour cream and hot sauce.",
                ],
                "tip": "Swap potatoes for leftover rice or quinoa ] both work great as a base.",
            },
            {
                "name": "Breakfast Sandwich",
                "image": "",
                "subtitle": "on an English muffin with cheese and hot sauce",
                "extras": [
                    "English muffin or brioche bun",
                    "1 slice cheddar or American cheese",
                    "Hot sauce or ketchup",
                    "Optional: cooked bacon or sausage patty",
                ],
                "steps": [
                    "Toast the English muffin until golden.",
                    "Cut an egg square to fit the muffin, place on the bottom half, and top with a cheese slice.",
                    "Microwave the open-faced sandwich 20–30 seconds to melt the cheese.",
                    "Add hot sauce and the top of the muffin. Press and eat.",
                ],
                "tip": "Freeze assembled sandwiches wrapped in foil [ reheat straight from frozen at 350°F for 25 minutes.",
            },
        ],
    },
    {
        "_id": "breakfast-snack-plate",
        "_keywords": ["eggs", "breakfast", "snack", "protein"],
        "image": "/static/images/breakfast-snack-plate.jpg",
        "intro": "The anti-recipe breakfast ] pull whatever you have from the fridge and arrange it on a plate. Aim for fat, protein, and something carby, and you're done. A great way to use up leftovers.",
        "base": {
            "title": "DIY Breakfast Snack Plates",
            "ingredients": [
                "A protein: hard-boiled egg, smoked salmon, deli meat, leftover chicken, cottage cheese, or Greek yogurt",
                "A fat: cheese, nuts, avocado, nut butter, or olive oil",
                "A carb: crackers, toast, fruit, leftover grains, or a granola bar",
                "Something fresh: cucumber slices, cherry tomatoes, berries, apple, or raw vegetables",
            ],
            "steps": [
                "Pick one item from each category above based on what you have.",
                "Arrange on a plate or layer into a container.",
                "That's it [ no cooking required. Prep 4-5 containers on Sunday and refrigerate up to 4 days.",
            ],
        },
        "uses": [
            {
                "name": "Smoked Salmon Plate",
                "subtitle": "with cucumber, hard-boiled egg, and everything bagel crackers",
                "extras": [
                    "2-3 oz smoked salmon",
                    "1 hard-boiled egg, halved",
                    "Sliced cucumber",
                    "Everything bagel crackers",
                    "Fresh dill, flaky salt, and pepper",
                ],
                "steps": [
                    "Arrange smoked salmon, halved egg, and cucumber slices on a plate.",
                    "Add crackers on the side. Finish with fresh dill and flaky salt.",
                ],
                "tip": None,
            },
            {
                "name": "Cottage Cheese Plate",
                "subtitle": "with tomatoes, cucumber, almonds, and dried fruit",
                "extras": [
                    "1/2 cup cottage cheese",
                    "Cherry tomatoes, halved",
                    "Sliced cucumber",
                    "Small handful almonds",
                    "A few pieces dried fruit (apricots, dates, or raisins)",
                    "Olive oil, dried herbs, salt, and pepper",
                ],
                "steps": [
                    "Spoon cottage cheese into the center of the plate.",
                    "Arrange tomatoes, cucumber, almonds, and dried fruit around it.",
                    "Drizzle cottage cheese with olive oil and a pinch of herbs.",
                ],
                "tip": None,
            },
            {
                "name": "Protein Plate",
                "subtitle": "with berries, cheese, and a hard-boiled egg",
                "extras": [
                    "1 hard-boiled egg",
                    "1 oz cheese cubes",
                    "1 cup fresh berries",
                    "1 protein bar, cut into pieces (or a handful of nuts)",
                ],
                "steps": [
                    "Arrange egg, cheese, berries, and protein bar pieces on a plate.",
                    "Eat as-is or refrigerate in a container for the morning rush.",
                ],
                "tip": "Swap in any leftover protein ] a few meatballs, a slice of quiche, or leftover salmon all work perfectly.",
            },
        ],
    },
    {
        "_id": "gourmet-toast",
        "_keywords": ["toast", "breakfast", "nut butter", "honey", "fruit", "sweet"],
        "image": "/static/images/sweet-toast.jpg",
        "intro": "Sweet, satisfying breakfast toasts built around a pantry of honey, nut butter, and nuts [ each variation swaps in a different fruit and spread so the whole batch stays interesting without needing many extra ingredients.",
        "base": {
            "title": "Sweet Gourmet Toast",
            "ingredients": [
                "4 slices sourdough or whole wheat bread",
                "1/4 cup nut butter (almond, cashew, or peanut ] your pick)",
                "3 tbsp honey",
                "1/4 cup mixed nuts, chopped (almonds, walnuts, or a mix)",
            ],
            "steps": [
                "Toast bread slices until golden and crisp.",
                "While still warm, proceed with your chosen topping variation below.",
                "Drizzle honey over the top before serving [ warmth from the toast helps it melt in.",
            ],
        },
        "uses": [
            {
                "name": "Nut Butter & Banana",
                "subtitle": "almond butter, banana slices, sliced almonds, honey",
                "extras": [
                    "2 tbsp almond butter (or cashew/peanut)",
                    "1 small banana, sliced",
                    "2 tbsp sliced almonds",
                    "Drizzle of honey",
                ],
                "steps": [
                    "Spread almond butter over warm toast.",
                    "Lay banana slices in a single layer on top.",
                    "Scatter sliced almonds and drizzle with honey.",
                ],
                "tip": "For extra richness, spread a thin layer of chocolate-hazelnut spread under the almond butter.",
            },
            {
                "name": "Apple, Walnut & Honey",
                "subtitle": "nut butter, honey, apple slices, chopped walnuts",
                "extras": [
                    "2 tbsp almond or walnut butter",
                    "1 tbsp honey (drizzled and slightly melted)",
                    "1/2 apple, thinly sliced",
                    "2 tbsp raw walnuts, roughly chopped",
                ],
                "steps": [
                    "Drizzle honey directly on warm toast and let it melt for 30 seconds.",
                    "Spread nut butter over the honey layer.",
                    "Fan apple slices across the top and scatter chopped walnuts.",
                    "Drizzle a little more honey to finish.",
                ],
                "tip": "Honeycrisp or Pink Lady apples hold their crunch best and aren't too tart.",
            },
            {
                "name": "Cottage Cheese & Berry",
                "subtitle": "cottage cheese, fresh berries, mint, crushed nuts, honey",
                "extras": [
                    "3 tbsp cottage cheese",
                    "1/3 cup fresh berries (strawberries, blueberries, or a mix)",
                    "A few fresh mint leaves",
                    "2 tbsp nuts, crushed (almonds, walnuts, or pistachios)",
                    "Drizzle of honey",
                ],
                "steps": [
                    "Spread cottage cheese generously over warm toast.",
                    "Top with fresh berries and a few torn mint leaves.",
                    "Scatter crushed nuts and finish with a honey drizzle.",
                ],
                "tip": "Swap cottage cheese for ricotta if you have it ] both work beautifully and share the same mild, creamy base.",
            },
        ],
    },
    {
        "_id": "savory-gourmet-toast",
        "_keywords": ["toast", "breakfast", "avocado", "ricotta", "mozzarella", "savory"],
        "image": "/static/images/gourmet-toast.jpg",
        "intro": "Three no-cook toast builds that go from fridge to plate in five minutes [ each one shares a base of good bread, a creamy spread, and fresh toppings, so you can rotate through them all week.",
        "base": {
            "title": "Savory Gourmet Toast",
            "ingredients": [
                "4 slices sourdough or whole wheat bread",
                "Flaky sea salt and black pepper",
                "Extra-virgin olive oil or good butter, for drizzling",
                "Red pepper flakes (optional)",
            ],
            "steps": [
                "Toast bread until golden and crisp ] a hot pan with a little butter gives great flavor.",
                "While still warm, build your chosen topping variation.",
                "Finish every version with a pinch of flaky salt and a drizzle of olive oil.",
            ],
        },
        "uses": [
            {
                "name": "Avocado & Tomato",
                "subtitle": "smashed avocado, heirloom tomato, lemon, red pepper flakes",
                "extras": [
                    "1 ripe avocado",
                    "1 small heirloom or roma tomato, sliced",
                    "Juice of 1/4 lemon",
                    "Red pepper flakes",
                    "Flaky salt and black pepper",
                ],
                "steps": [
                    "Mash avocado with lemon juice, salt, and pepper until creamy but still chunky.",
                    "Spread generously on warm toast.",
                    "Layer tomato slices on top and finish with red pepper flakes and flaky salt.",
                ],
                "tip": "A sprinkle of everything bagel seasoning works great here instead of the pepper flakes.",
            },
            {
                "name": "Ricotta, Tomato & Basil",
                "subtitle": "whipped ricotta, sliced tomato, fresh basil, balsamic glaze",
                "extras": [
                    "3 tbsp whole-milk ricotta",
                    "1 roma or heirloom tomato, sliced thin",
                    "5–6 fresh basil leaves, torn",
                    "Balsamic glaze, for drizzling",
                    "Flaky salt and olive oil",
                ],
                "steps": [
                    "Spread ricotta thickly over warm toast [ use the back of a spoon to swirl it.",
                    "Lay tomato slices over the ricotta and scatter torn basil.",
                    "Drizzle with balsamic glaze and olive oil, then finish with flaky salt.",
                ],
                "tip": "Ricotta firms up quickly ] build these right before eating for the best texture.",
            },
            {
                "name": "Mozzarella, Pesto & Balsamic",
                "subtitle": "fresh mozzarella, basil pesto, cherry tomatoes, balsamic glaze",
                "extras": [
                    "2 oz fresh mozzarella, sliced",
                    "2 tbsp basil pesto (store-bought is fine)",
                    "A handful of cherry tomatoes, halved",
                    "Balsamic glaze, for drizzling",
                    "Fresh basil leaves",
                ],
                "steps": [
                    "Spread pesto over warm toast.",
                    "Layer mozzarella slices and halved cherry tomatoes on top.",
                    "Drizzle with balsamic glaze and top with fresh basil leaves.",
                ],
                "tip": "Broil for 90 seconds if you want the mozzarella to melt and the edges to blister.",
            },
        ],
    },
    {
        "_id": "chicken-parm-meatballs",
        "_keywords": ["chicken", "meatball", "parm"],
        "image": "/static/images/chicken-parm-meatballs-recipe.jpg",
        "intro": "All the bubbly cheese and rich marinara of chicken Parmesan, none of the breading or frying [ one skillet, 30 minutes, and you have meatballs that become three completely different meals.",
        "base": {
            "title": "Chicken Parm Meatballs",
            "ingredients": [
                "4 slices white bread, torn into small pieces",
                "1/2 cup water",
                "2 large eggs",
                "1 lb ground chicken",
                "1/4 cup finely grated Parmesan, plus more for serving",
                "2 tbsp fresh parsley, minced",
                "1 clove garlic, grated",
                "1 tsp kosher salt",
                "Freshly ground black pepper",
                "3 tbsp extra-virgin olive oil",
                "One 16-oz jar marinara sauce",
                "3-4 slices provolone cheese",
                "Fresh basil, for garnish",
            ],
            "steps": [
                "Soak torn bread in 1/2 cup water for 3 minutes until soft. Whisk in the eggs.",
                "Gently combine the bread mixture with ground chicken, Parmesan, parsley, garlic, salt, and pepper. Do not overmix.",
                "Form into 12 meatballs.",
                "Heat olive oil in a large oven-safe skillet over medium-high. Brown meatballs on all sides, about 6-8 minutes total.",
                "Reduce heat to low. Add marinara sauce and simmer 8-10 minutes until meatballs are cooked through (165 F internal).",
                "Preheat broiler to high. Lay provolone slices over the meatballs and broil 2 minutes until melted and bubbling.",
                "Finish with extra Parmesan and fresh basil.",
            ],
        },
        "uses": [
            {
                "name": "Meatballs with Garlic Bread",
                "subtitle": "straight from the skillet with crusty bread for dipping",
                "extras": [
                    "1 baguette or ciabatta loaf, sliced",
                    "3 tbsp butter, softened",
                    "2 cloves garlic, minced",
                    "2 tbsp fresh parsley, chopped",
                ],
                "steps": [
                    "Mix butter with minced garlic and parsley. Spread on sliced bread.",
                    "Broil bread 2-3 minutes until golden.",
                    "Serve meatballs straight from the skillet with garlic bread alongside for scooping up the sauce.",
                ],
                "tip": "Use any extra marinara sauce in the pan for dipping.",
            },
            {
                "name": "Pasta with Meatballs",
                "subtitle": "with spaghetti or rigatoni and extra Parmesan",
                "extras": [
                    "8 oz spaghetti or rigatoni",
                    "Extra Parmesan and fresh basil, to finish",
                ],
                "steps": [
                    "Cook pasta in salted boiling water until al dente. Drain, reserving 1/4 cup pasta water.",
                    "Toss pasta with the meatball sauce, adding a splash of pasta water to loosen.",
                    "Pile pasta into bowls and top with meatballs, extra Parmesan, and fresh basil.",
                ],
                "tip": None,
            },
            {
                "name": "Meatball Sub",
                "subtitle": "on a toasted hoagie with mozzarella and marinara",
                "extras": [
                    "Hoagie rolls or sub rolls",
                    "Shredded mozzarella or extra provolone",
                    "Extra marinara sauce",
                ],
                "steps": [
                    "Split hoagie rolls and toast cut-side up under the broiler 1-2 minutes.",
                    "Nestle 3-4 meatballs into each roll. Spoon extra marinara over the top and add cheese.",
                    "Broil 1-2 minutes until cheese is melted and bubbly.",
                ],
                "tip": "Warm meatballs in marinara on the stovetop first if reheating from the fridge.",
            },
        ],
    },
    # ── Italian cuisine recipes ───────────────────────────────────────────────
    {
        "_id": "broccoli-pasta",
        "_keywords": ["broccoli", "pasta"],
        "image": "/static/images/broccoli-pasta.jpg",
        "intro": "Cook broccoli in the pasta water, mash it into a silky garlic sauce, and finish with toasted breadcrumbs and lemon ] one pot, 30 minutes, and the same base makes a creamy soup or a crispy breadcrumb pasta.",
        "base": {
            "title": "Pasta con i Broccoli",
            "ingredients": [
                "400g (14 oz) short pasta [ rigatoni, fusilli, or penne",
                "1 lb (450g) broccoli, cut into small florets",
                "3 tbsp extra virgin olive oil",
                "3 garlic cloves, minced",
                "½ tsp chili flakes (optional)",
                "½ lemon, juiced",
                "Salt and black pepper to taste",
                "½ cup pasta water (reserved)",
                "3 tbsp breadcrumbs",
                "½ tbsp olive oil (for breadcrumbs)",
                "Grated Parmesan or Pecorino, for serving",
            ],
            "steps": [
                "Bring a large pot of heavily salted water to a boil.",
                "Toast breadcrumbs: in a skillet over low heat, warm ½ tbsp olive oil and fry breadcrumbs with a pinch of salt for 4–5 minutes until golden and crispy. Transfer to a plate.",
                "Add broccoli florets to the boiling water and cook 5 minutes until tender. Remove with a slotted spoon into a bowl ] keep the water boiling.",
                "Cook pasta in the same broccoli water until al dente. Reserve ½ cup pasta water before draining.",
                "In the same skillet over low heat, warm 3 tbsp olive oil. Add garlic and chili flakes and cook 1 minute until fragrant [ don't let it brown.",
                "Add cooked broccoli and the reserved pasta water to the skillet. Simmer over medium heat 3–4 minutes, breaking up the broccoli with a spoon into a rough, saucy texture.",
                "Season well with salt and pepper. Add drained pasta and toss until fully coated. Add a splash more pasta water if needed.",
                "Finish with lemon juice, a drizzle of olive oil, and the toasted breadcrumbs. Serve with Parmesan.",
            ],
        },
        "uses": [
            {
                "name": "Pasta con i Broccoli",
                "image": "",
                "subtitle": "with crispy breadcrumbs, lemon, and Parmesan",
                "extras": [
                    "Extra virgin olive oil, for serving",
                    "Grated Parmesan or Pecorino, for serving",
                    "Toasted breadcrumbs",
                    "Lemon wedge",
                ],
                "steps": [
                    "Make the base recipe through step 8.",
                    "Divide into bowls and top with extra breadcrumbs, a shower of Parmesan, and a squeeze of lemon.",
                ],
                "tip": "Reheat leftovers by sautéing in a pan with a splash of water ] the pasta gets lightly crispy and even better.",
            },
            {
                "name": "Vellutata di Broccoli",
                "image": "",
                "subtitle": "silky cream of broccoli soup, no cream needed",
                "extras": [
                    "1 small onion, roughly chopped",
                    "2 cups vegetable stock",
                    "Extra virgin olive oil, for serving",
                    "Crispy breadcrumbs and Parmesan, for topping",
                ],
                "steps": [
                    "Cook garlic and onion in olive oil 3 minutes. Add broccoli and stock, simmer 15 minutes.",
                    "Blend until silky with an immersion blender. Season and cool.",
                    "Reheat in a saucepan and ladle into bowls. Top with a drizzle of olive oil, Parmesan, and crispy breadcrumbs.",
                ],
                "tip": "Freeze in portions for up to 3 months. Pairs perfectly with crusty bread.",
            },
            {
                "name": "Crispy Breadcrumb Pasta",
                "image": "",
                "subtitle": "aglio e olio style with golden pangrattato",
                "extras": [
                    "Extra toasted breadcrumbs",
                    "Red pepper flakes",
                    "Extra virgin olive oil",
                ],
                "steps": [
                    "Follow the base recipe but skip the Parmesan.",
                    "Toss pasta with extra olive oil, red pepper flakes, and a very generous handful of toasted breadcrumbs.",
                ],
                "tip": "Breadcrumbs keep in the freezer for weeks [ toast a big batch and use them on soups, salads, and grilled fish too.",
            },
        ],
    },
    {
        "_id": "tortellini-soup",
        "_keywords": ["tortellini"],
        "image": "/static/images/Spicy-Sausage-and-Tortellini.jpg",
        "intro": "One pot, 35 minutes ] spicy Italian sausage, cheese tortellini, kale, and fire-roasted tomatoes come together in a deeply satisfying soup that packs beautifully for the week.",
        "base": {
            "title": "Spicy Italian Sausage and Tortellini Soup",
            "ingredients": [
                "1/2 lb spicy Italian pork sausage",
                "1 cup julienned red bell pepper",
                "1/2 cup chopped sweet white onion",
                "8 oz gluten-free cheese tortellini",
                "4 cups chicken stock",
                "1 (15 oz) can fire-roasted tomatoes",
                "2 tbsp tomato paste",
                "1 tsp dried oregano",
                "1/2 tsp sea salt",
                "2 cups chopped kale",
                "1/3 cup heavy cream",
                "1/4 cup shaved Parmesan cheese",
                "1/3 cup chopped fresh basil",
            ],
            "steps": [
                "Heat a 4-quart Dutch oven over medium heat. Add spicy Italian pork sausage and brown for 5 minutes, breaking it into crumbles with a wooden spoon.",
                "Add onion and bell pepper and cook another 5 minutes, stirring occasionally.",
                "Stir in oregano, sea salt, tomato paste, fire-roasted tomatoes, and chicken stock. Bring to a boil and cook 10 minutes.",
                "Stir in kale and tortellini and boil 5 more minutes.",
                "Turn off heat and slowly stir in heavy cream.",
            ],
        },
        "uses": [
            {
                "name": "Soup Bowl",
                "subtitle": "with shaved Parmesan and fresh basil",
                "extras": [
                    "Shaved Parmesan, for serving",
                    "Chopped fresh basil, for serving",
                ],
                "steps": [
                    "Ladle soup into bowls.",
                    "Top with shaved Parmesan and freshly chopped basil.",
                ],
                "tip": "Tortellini will absorb more liquid as it sits [ add a splash of stock when reheating.",
            },
            {
                "name": "Soup with Crusty Bread",
                "subtitle": "served alongside garlic-rubbed ciabatta",
                "extras": [
                    "1 ciabatta loaf, sliced and toasted",
                    "1 garlic clove, halved",
                    "Olive oil, for drizzling",
                ],
                "steps": [
                    "Toast ciabatta slices under the broiler 2–3 minutes until golden.",
                    "Rub each slice with the cut side of a garlic clove and drizzle with olive oil.",
                    "Serve alongside a generous bowl of soup.",
                ],
                "tip": None,
            },
            {
                "name": "Meal Prep Containers",
                "subtitle": "4 single-portion servings with Parmesan and basil on top",
                "extras": [
                    "Shaved Parmesan, for topping",
                    "Chopped fresh basil, for topping",
                ],
                "steps": [
                    "Divide soup evenly into 4 single-compartment meal prep containers.",
                    "Once cooled, top each with Parmesan and basil.",
                    "Refrigerate up to 4 days. Reheat with a splash of chicken stock if needed.",
                ],
                "tip": "Freezes well for up to 3 months ] skip the cream if freezing and stir it in when reheating.",
            },
        ],
    },
    {
        "_id": "italian-couscous",
        "_keywords": ["couscous"],
        "image": "/static/images/Italian-Couscous.jpg",
        "intro": "A hearty, no-cook Italian salad that gets better as it sits [ roasted garlic couscous loaded with salami, mozzarella, chickpeas, and a punchy red wine vinaigrette.",
        "base": {
            "title": "Italian Couscous Salad",
            "ingredients": [
                "2 (4.7 oz) packages roasted garlic and olive oil couscous (e.g. Near East brand)",
                "1 (15 oz) can chickpeas, drained",
                "5 oz Genoa salami, coarsely chopped",
                "5 oz fresh mozzarella (bocconcini), chopped into bite-sized pieces",
                "1 large green bell pepper, coarsely chopped",
                "5 oz black olives, sliced or halved",
                "2 cups cherry tomatoes, sliced or halved",
                "3/4 cup fresh basil, chiffonade",
                "1/3 cup olive oil",
                "1/3 cup red wine vinegar",
                "1 tbsp Dijon mustard",
                "1 tsp honey",
                "1 tsp minced garlic",
                "1/2 tsp dried basil",
                "1/2 tsp dried parsley",
                "1/2 tsp dried oregano",
                "1/4 tsp red pepper flakes (optional)",
                "Salt and pepper to taste",
                "Lemon wedges, for serving",
            ],
            "steps": [
                "Prepare couscous according to package instructions, including the seasoning mix. Let cool completely.",
                "Drain chickpeas. Chop salami, cut mozzarella into bite-sized pieces, slice bell pepper, halve olives, and halve cherry tomatoes. Chiffonade basil by stacking leaves, rolling tightly, and slicing thin.",
                "Make the dressing: combine olive oil, red wine vinegar, Dijon, honey, garlic, dried basil, parsley, oregano, red pepper flakes, salt, and pepper in a jar. Shake well.",
            ],
        },
        "uses": [
            {
                "name": "Italian Couscous Salad",
                "subtitle": "tossed with red wine vinaigrette and a squeeze of lemon",
                "extras": [
                    "Freshly squeezed lemon",
                    "Extra salt and pepper",
                ],
                "steps": [
                    "Add cooled couscous to a large bowl with all veggies, salami, mozzarella, basil, and chickpeas.",
                    "Add dressing, a squeeze of lemon, and season to taste. Toss and serve immediately.",
                ],
                "tip": "Only dress what you plan to eat right away ] this salad doesn't sit well once dressed.",
            },
            {
                "name": "Meal Prep Jars",
                "subtitle": "6 containers with dressing on the side",
                "extras": [
                    "6 small dressing containers",
                    "6 lemon wedges",
                ],
                "steps": [
                    "Divide dressing evenly into 6 small separate containers.",
                    "Divide couscous, veggies, salami, mozzarella, basil, and chickpeas evenly among 6 meal prep containers.",
                    "Add a lemon wedge and a pinch of salt and pepper to each. To serve, add dressing, squeeze lemon, stir, and eat.",
                ],
                "tip": "Dressed salad keeps 1–2 days in the fridge. Undressed keeps 4–5 days.",
            },
            {
                "name": "Couscous Bowl with Grilled Chicken",
                "subtitle": "topped with sliced chicken and extra vinaigrette",
                "extras": [
                    "2 boneless skinless chicken breasts",
                    "Olive oil, salt, pepper, Italian seasoning",
                ],
                "steps": [
                    "Season chicken with olive oil, salt, pepper, and Italian seasoning. Grill or pan-sear 6–7 minutes per side until cooked through (165 F internal).",
                    "Rest 5 minutes then slice.",
                    "Serve sliced chicken over a generous scoop of couscous salad with a drizzle of extra dressing.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "tuscan-chicken-pasta",
        "_keywords": ["tuscan", "chicken"],
        "image": "/static/images/Spicy-Tuscan-Chicken-Pasta.jpg",
        "intro": "A restaurant-worthy pasta that comes together in 30 minutes [ juicy chicken, sun-dried tomatoes, wilted spinach, and a creamy Parmesan sauce clinging to every piece of pasta.",
        "base": {
            "title": "Spicy Tuscan Chicken Pasta",
            "ingredients": [
                "1.5 lbs boneless skinless chicken breasts, pounded to even thickness",
                "2 tsp Italian seasoning",
                "1 tsp smoked paprika",
                "1/2 tsp red pepper flakes, plus more to taste",
                "Salt and black pepper",
                "3 tbsp olive oil",
                "5 cloves garlic, minced",
                "1/2 cup sun-dried tomatoes in oil, drained and roughly chopped",
                "1 cup chicken broth",
                "1 cup heavy cream",
                "1/2 cup grated Parmesan",
                "3 cups baby spinach",
                "12 oz penne or rigatoni",
                "Fresh basil and extra Parmesan, to serve",
            ],
            "steps": [
                "Cook pasta in heavily salted boiling water until al dente. Reserve 1/2 cup pasta water before draining.",
                "Season chicken with Italian seasoning, smoked paprika, red pepper flakes, salt, and pepper.",
                "Heat 2 tbsp olive oil in a large skillet over medium-high heat. Cook chicken 5–6 minutes per side until golden and cooked through (165 F internal). Rest 5 minutes, then slice into strips.",
                "In the same skillet, heat remaining 1 tbsp olive oil over medium heat. Cook garlic 1 minute until fragrant. Add sun-dried tomatoes and stir 1 minute.",
                "Pour in chicken broth and scrape up any browned bits. Add heavy cream and bring to a simmer. Cook 3–4 minutes until slightly thickened.",
                "Stir in Parmesan until melted. Add spinach and stir until wilted, 1–2 minutes.",
                "Add drained pasta and sliced chicken to the skillet. Toss to coat, adding pasta water a splash at a time to loosen the sauce.",
            ],
        },
        "uses": [
            {
                "name": "Tuscan Chicken Pasta Bowl",
                "subtitle": "with fresh basil and extra Parmesan",
                "extras": [
                    "Fresh basil, torn",
                    "Extra grated Parmesan",
                    "Red pepper flakes",
                ],
                "steps": [
                    "Divide pasta into bowls.",
                    "Top with torn fresh basil, extra Parmesan, and a pinch of red pepper flakes.",
                ],
                "tip": "The sauce thickens as it sits ] add a splash of broth or cream when reheating.",
            },
            {
                "name": "Leftover Baked Pasta",
                "subtitle": "baked with mozzarella until bubbly",
                "extras": [
                    "1 cup shredded mozzarella",
                    "Extra Parmesan for topping",
                ],
                "steps": [
                    "Transfer leftover pasta to an oven-safe dish.",
                    "Top with shredded mozzarella and Parmesan.",
                    "Bake at 375 F for 20 minutes until bubbly and golden.",
                ],
                "tip": None,
            },
            {
                "name": "Tuscan Chicken Salad",
                "subtitle": "leftover chicken over arugula with lemon and Parmesan",
                "extras": [
                    "3 cups arugula",
                    "Juice of 1 lemon",
                    "2 tbsp olive oil",
                    "Shaved Parmesan",
                    "Cherry tomatoes, halved",
                ],
                "steps": [
                    "Toss arugula with lemon juice, olive oil, salt, and pepper.",
                    "Arrange in bowls and top with sliced leftover Tuscan chicken, cherry tomatoes, and shaved Parmesan.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "pasta-alla-gricia",
        "_keywords": ["gricia", "guanciale", "pecorino", "roman", "italian pasta"],
        "image": "/static/images/Pasta-Alla-Gricia-Recipe.jpg",
        "intro": "The Roman pasta that came before carbonara [ guanciale rendered until golden, black pepper bloomed in the fat, and Pecorino Romano stirred into a glossy, emulsified sauce. Four ingredients, maximum flavor.",
        "base": {
            "title": "Pasta alla Gricia",
            "ingredients": [
                "8 oz guanciale (or pancetta/bacon if unavailable)",
                "Fine sea salt",
                "1 tsp freshly ground black pepper, plus more to taste",
                "12 oz mezzi rigatoni or rigatoni",
                "2 oz Pecorino Romano, very finely grated by hand",
            ],
            "steps": [
                "Slice guanciale into ¼-inch pieces, then cut into roughly ½ × 1-inch pieces. (Freeze 10 minutes first for easier cutting.)",
                "Heat a large sauté pan over medium-low. Add guanciale and cook, stirring occasionally, until golden and crispy with rendered fat, about 10 minutes. Transfer guanciale to a plate. Pour rendered fat into a measuring cup, return ¼ cup to the pan, and discard the rest.",
                "Add 1 tsp black pepper to the hot fat. Turn off the heat and let the pepper bloom in the residual warmth.",
                "Bring a large pot of water to a boil. Add 1 tbsp salt and the pasta. Cook for 8 minutes, stirring occasionally.",
                "At the 6-minute mark, scoop 1½ cups starchy pasta water into the sauté pan. Turn heat to high and bring to a boil, stirring gently to emulsify the water and fat as the sauce reduces.",
                "At the 8-minute mark, transfer pasta directly into the pan using tongs or a spider strainer. Toss continuously for 3–5 minutes until the sauce emulsifies and clings to the pasta, adding pasta water a splash at a time if the sauce gets too tight.",
                "Turn off heat. Stir in the guanciale and half the Pecorino. Add remaining Pecorino and toss gently until the sauce is glossy. Loosen with a few tablespoons of pasta water if needed.",
                "Serve immediately with extra Pecorino and black pepper.",
            ],
        },
        "uses": [
            {
                "name": "Classic Gricia Bowl",
                "image": "",
                "subtitle": "topped with extra Pecorino and cracked black pepper",
                "extras": [
                    "Extra Pecorino Romano, for serving",
                    "Freshly cracked black pepper",
                ],
                "steps": [
                    "Follow the base recipe through step 8.",
                    "Divide into warm bowls and shower with extra Pecorino and a generous crack of pepper.",
                ],
                "tip": "Use the finest grater you have ] pre-grated Pecorino won't melt into the sauce properly.",
            },
            {
                "name": "Gricia-Inspired Leftovers",
                "image": "",
                "subtitle": "reheated in a pan with a little pasta water",
                "extras": [
                    "2–3 tbsp water or pasta water",
                    "Extra Pecorino Romano",
                ],
                "steps": [
                    "Add leftover pasta to a pan over medium-low heat with a splash of water.",
                    "Toss until heated through and sauce loosens. Add more water as needed.",
                    "Top with fresh Pecorino and pepper before serving.",
                ],
                "tip": "Never microwave [ reheating in a pan revives the sauce and keeps the guanciale crispy.",
            },
        ],
    },
    {
        "_id": "chicken-burrito-bowl",
        "_keywords": ["burrito", "mexican", "chicken", "rice", "avocado"],
        "image": "/static/images/Chicken-Burrito-Bowl.jpg",
        "intro": "Cumin-spiced baked chicken and cilantro rice prepped once ] then build burrito bowls, stuff into burritos, or wrap in lettuce all week. Fresh, filling, and endlessly customizable.",
        "base": {
            "title": "Chicken Burrito Bowl",
            "ingredients": [
                "1 lb boneless, skinless chicken breasts",
                "1/4 tsp cumin",
                "1/2 tsp garlic powder, divided",
                "Salt and pepper to taste",
                "1 cup long-grain white rice",
                "2 cups water",
                "1/2 bunch cilantro, chopped",
                "2 avocados, diced",
                "1 pint cherry tomatoes, halved",
                "8 oz sliced black olives",
                "1 jar salsa",
                "4 oz shredded cheddar cheese",
            ],
            "steps": [
                "Preheat oven to 375°F. Place chicken on a parchment-lined sheet pan and season with salt, pepper, and 1/4 tsp garlic powder.",
                "Bake 25 minutes until the internal temperature reaches 165°F. Let cool, then cut or shred into bite-sized pieces.",
                "Meanwhile, combine rice and water in a medium pot. Bring to a boil, cover, and simmer 25 minutes until water is absorbed. Stir in salt, cumin, and remaining 1/4 tsp garlic powder.",
                "Halve cherry tomatoes, dice avocados, and chop cilantro.",
                "Store all components separately in the fridge [ assemble each bowl fresh throughout the week.",
            ],
        },
        "uses": [
            {
                "name": "Burrito Bowl",
                "image": "",
                "subtitle": "layered over cilantro rice with all the toppings",
                "extras": [
                    "Sour cream (optional)",
                    "Lime wedge",
                    "Hot sauce",
                ],
                "steps": [
                    "Scoop cilantro rice into a bowl.",
                    "Top with chicken, cherry tomatoes, avocado, black olives, and a spoonful of salsa.",
                    "Finish with cheddar, a dollop of sour cream, a squeeze of lime, and hot sauce.",
                ],
                "tip": "Add a scoop of black beans or corn for extra bulk.",
            },
            {
                "name": "Burrito",
                "image": "",
                "subtitle": "wrapped tight in a large flour tortilla",
                "extras": [
                    "Large flour tortillas (10-inch)",
                    "Sour cream",
                    "Lime wedge",
                ],
                "steps": [
                    "Warm a large flour tortilla in a dry pan or microwave 20 seconds.",
                    "Layer rice, chicken, tomatoes, avocado, olives, salsa, and cheddar down the center.",
                    "Fold in the sides, roll up tightly, and press seam-side down.",
                    "Optional: toast in a dry pan 1–2 minutes per side until golden and sealed.",
                ],
                "tip": "Don't overfill ] less is more for a burrito that actually stays together.",
            },
            {
                "name": "Lettuce Wrap",
                "image": "",
                "subtitle": "in crisp romaine or butter lettuce leaves",
                "extras": [
                    "8 large romaine or butter lettuce leaves",
                    "Lime wedge",
                    "Hot sauce",
                ],
                "steps": [
                    "Lay out large lettuce leaves on a plate.",
                    "Spoon a small amount of rice (or skip it) into each leaf, then add chicken, tomatoes, avocado, olives, and salsa.",
                    "Top with cheddar and a squeeze of lime.",
                ],
                "tip": "Skip the rice to keep it low-carb. Double the avocado to make up for it.",
            },
        ],
    },
    {
        "_id": "ground-turkey-base",
        "_keywords": ["turkey", "taco", "mexican", "ground turkey", "stuffed peppers"],
        "image": "/static/images/Turkey-Taco-Soup.jpg",
        "intro": "One pound of ground turkey seasoned with taco spices and cooked down with onion, bell peppers, carrots, mushrooms, and jalapeño [ ladle it into soup, spoon it into bell peppers, or pile it into tacos.",
        "base": {
            "title": "Taco-Spiced Ground Turkey",
            "ingredients": [
                "1 lb ground turkey breast",
                "1 yellow onion, finely chopped",
                "3 bell peppers (red, green, yellow), finely chopped",
                "2 carrots, finely chopped",
                "2 cloves garlic, minced",
                "1 jalapeño, seeds removed, finely chopped",
                "8 oz sliced mushrooms",
                "1 tsp olive oil",
                "2 tbsp taco seasoning",
                "2 cups tomato puree",
                "3½ cups low-sodium vegetable broth",
            ],
            "steps": [
                "Finely chop the onion, carrots, bell peppers, garlic, and jalapeño.",
                "Heat olive oil in a large pot over medium-high heat. Add vegetables, mushrooms, and ground turkey.",
                "Cook about 10 minutes, breaking the turkey apart with a spoon, until the meat is cooked through and the vegetables are softened.",
                "Add taco seasoning, tomato puree, and vegetable broth. Stir to combine.",
                "Reduce heat to low and simmer 30 minutes.",
                "Store in the fridge up to 4 days, or freeze in portions up to 3 months.",
            ],
        },
        "uses": [
            {
                "name": "Taco Soup",
                "image": "",
                "subtitle": "with avocado, cheddar, and sour cream",
                "extras": [
                    "1 avocado, sliced",
                    "Shredded cheddar cheese",
                    "Sour cream or Greek yogurt",
                    "Tortilla chips (optional)",
                ],
                "steps": [
                    "Ladle the turkey base (with all the broth) into bowls ] this is the soup as-is.",
                    "Top with sliced avocado, a handful of cheddar, and a dollop of sour cream.",
                    "Serve with tortilla chips on the side for dipping.",
                ],
                "tip": "The soup thickens as it sits in the fridge [ just add a splash of broth when reheating.",
            },
            {
                "name": "Stuffed Bell Peppers",
                "image": "",
                "subtitle": "filled with the turkey base and baked with melted cheese",
                "extras": [
                    "4 large bell peppers, halved and seeded",
                    "1 cup cooked rice (optional, to bulk)",
                    "1 cup shredded cheddar or Monterey Jack",
                ],
                "steps": [
                    "Preheat oven to 375°F.",
                    "Halve and seed 4 bell peppers and arrange cut-side up in a baking dish.",
                    "Mix the turkey base with cooked rice if using. Spoon into each pepper half.",
                    "Top with shredded cheese and bake 25–30 minutes until the peppers are tender and cheese is bubbly.",
                ],
                "tip": "Use the leftover broth from the base as a sauce ] pour a little into the baking dish before cooking.",
            },
            {
                "name": "Tacos",
                "image": "",
                "subtitle": "in corn or flour tortillas with classic toppings",
                "extras": [
                    "Corn or flour tortillas",
                    "Shredded cheddar",
                    "Sour cream",
                    "Salsa",
                    "Avocado or guacamole",
                    "Lime wedge",
                ],
                "steps": [
                    "Warm tortillas in a dry pan or directly over a gas flame until lightly charred.",
                    "Spoon the turkey mixture (drained of most broth) into each tortilla.",
                    "Top with cheddar, sour cream, salsa, avocado, and a squeeze of lime.",
                ],
                "tip": "Drain excess liquid from the turkey before filling the tacos so they don't get soggy.",
            },
        ],
    },
    {
        "_id": "guac-stuffed-peppers",
        "_keywords": ["guacamole", "avocado", "peppers", "snack", "mexican", "nachos"],
        "image": "/static/images/Guacamole-Mini-Peppers.jpg",
        "intro": "Fresh lime guacamole stuffed into mini pepper halves [ make the guac once and it becomes a snack plate, a nacho topping, or a light lunch with carrots and peppers all week.",
        "base": {
            "title": "Guacamole Stuffed Mini Peppers",
            "ingredients": [
                "2 ripe avocados",
                "2 tbsp red onion, finely diced",
                "1 lime, juiced",
                "Salt to taste",
                "Optional: pinch of cumin, chopped cilantro, or diced tomato",
                "4 mini bell peppers, halved and de-stemmed",
            ],
            "steps": [
                "Wash mini peppers, halve them, and remove the stem and any seeds. Pat dry.",
                "Halve avocados, remove pits, and scoop flesh into a bowl. Mash with a fork to your preferred texture.",
                "Stir in red onion, lime juice, and salt. Taste and adjust.",
                "Spoon guacamole into pepper halves and serve immediately, or prep components separately and assemble before eating.",
                "Store leftover guacamole with the avocado pits pressed on the surface and wrap tightly ] keeps 1–2 days.",
            ],
        },
        "uses": [
            {
                "name": "Stuffed Mini Peppers",
                "image": "",
                "subtitle": "guacamole piled into mini pepper halves",
                "extras": [
                    "Extra lime wedge",
                    "Flaky salt",
                ],
                "steps": [
                    "Spoon guacamole generously into each pepper half.",
                    "Finish with a squeeze of lime and a pinch of flaky salt.",
                ],
                "tip": "Prep the peppers and guac separately [ stuff just before eating so the peppers stay crisp.",
            },
            {
                "name": "Snack Plate",
                "image": "",
                "subtitle": "guac with mini peppers, carrots, and whatever else you have",
                "extras": [
                    "Handful of baby carrots",
                    "Extra mini peppers or sliced cucumber",
                    "Tortilla chips (optional)",
                ],
                "steps": [
                    "Scoop guacamole into a small bowl and place in the center of a plate.",
                    "Arrange baby carrots, mini peppers, and any other raw veggies around it.",
                    "Add a handful of tortilla chips on the side if you want something crunchy.",
                ],
                "tip": "This also works as a light lunch ] add a hard-boiled egg or a handful of nuts to round it out.",
            },
            {
                "name": "Nachos",
                "image": "",
                "subtitle": "loaded tortilla chips with guac and toppings",
                "extras": [
                    "Tortilla chips",
                    "Shredded cheddar or Monterey Jack",
                    "Salsa",
                    "Sour cream",
                    "Sliced jalapeños (optional)",
                ],
                "steps": [
                    "Spread tortilla chips in a single layer on a sheet pan.",
                    "Top with shredded cheese and bake at 375°F for 5–7 minutes until melted.",
                    "Remove from oven and dollop guacamole, salsa, and sour cream over the top.",
                    "Add jalapeños and serve immediately.",
                ],
                "tip": "Add guac after baking [ heat turns it brown and bitter.",
            },
        ],
    },
    {
        "_id": "chicken-katsudon",
        "_keywords": ["katsu", "katsudon", "japanese", "chicken", "breaded", "rice bowl"],
        "image": "/static/images/chicken-katsudon.jpg",
        "intro": "Panko-crusted chicken cutlet fried golden, then simmered in a sweet dashi-soy egg sauce and served over steamed rice ] the same crispy katsu also fills a sando sandwich or tops a crisp salad.",
        "base": {
            "title": "Chicken Katsu Don",
            "ingredients": [
                "2 boneless, skinless chicken thighs or breasts",
                "Salt and white pepper",
                "1/2 cup plain flour",
                "1 egg, beaten",
                "1 cup panko breadcrumbs",
                "Neutral oil, for shallow frying (about 1cm deep)",
                "[ Dashi egg sauce ]",
                "180ml dashi stock (or 1 tsp dashi powder dissolved in 180ml hot water)",
                "2 tbsp soy sauce",
                "2 tbsp mirin",
                "1 tsp sugar",
                "3 eggs, lightly beaten",
                "1/2 onion, thinly sliced",
                "[ To serve ]",
                "2 cups cooked short-grain Japanese rice",
                "2 spring onions, finely sliced",
                "Nori strips and sesame seeds (optional)",
            ],
            "steps": [
                "Place chicken between two sheets of plastic wrap and pound to an even 1cm thickness. Season with salt and white pepper.",
                "Set up a crumbing station: flour, beaten egg, then panko. Coat each piece [ flour first, then egg, then press firmly into panko.",
                "Heat oil in a frying pan over medium-high heat. Fry chicken 3–4 minutes per side until deep golden and cooked through. Drain on paper towel, rest 2 minutes, then slice.",
                "In a small pan, combine dashi, soy sauce, mirin, and sugar. Bring to a simmer, add sliced onion, and cook 3 minutes until softened.",
                "Pour beaten eggs over the simmering sauce. Cover and cook on low 60–90 seconds until just set but still slightly runny in the centre.",
                "Scoop rice into bowls. Slide the egg and sauce over the rice, then top with sliced katsu, spring onions, and nori.",
            ],
        },
        "uses": [
            {
                "name": "Katsu Don Bowl",
                "image": "",
                "subtitle": "crispy katsu over rice with a silky dashi egg sauce",
                "extras": [
                    "Spring onions, sliced",
                    "Nori strips",
                    "Sesame seeds",
                ],
                "steps": [
                    "Follow the base recipe through step 6.",
                    "Garnish with spring onions, nori strips, and sesame seeds.",
                ],
                "tip": "The egg sauce should be just barely set ] it continues cooking from residual heat, so pull it early.",
            },
            {
                "name": "Katsu Sando",
                "image": "",
                "subtitle": "crispy katsu in milk bread with tonkatsu sauce and cabbage",
                "extras": [
                    "4 slices Japanese milk bread or soft white sandwich bread",
                    "2 tbsp tonkatsu sauce (or ketchup + Worcestershire)",
                    "1 cup finely shredded cabbage",
                    "Japanese mayo",
                ],
                "steps": [
                    "Spread tonkatsu sauce on one slice of bread, Japanese mayo on the other.",
                    "Layer shredded cabbage, then the sliced katsu cutlet.",
                    "Press the sandwich firmly, trim crusts, and slice in half.",
                ],
                "tip": "Wrap tightly in cling wrap and rest 5 minutes before slicing [ it holds together better.",
            },
            {
                "name": "Katsu Salad",
                "image": "",
                "subtitle": "sliced katsu over shredded cabbage with sesame dressing",
                "extras": [
                    "2 cups shredded green cabbage",
                    "1 cucumber, thinly sliced",
                    "2 tbsp sesame dressing (or rice vinegar + sesame oil + soy)",
                    "Sesame seeds",
                ],
                "steps": [
                    "Toss shredded cabbage and cucumber with sesame dressing.",
                    "Top with sliced katsu and a sprinkle of sesame seeds.",
                    "Drizzle a little extra tonkatsu sauce or Japanese mayo over the katsu.",
                ],
                "tip": "Dress the salad right before serving so the cabbage stays crisp.",
            },
        ],
    },
    {
        "_id": "tuna-sushi",
        "_keywords": ["tuna", "sushi", "japanese", "maki", "rice bowl", "nori"],
        "image": "/static/images/tuna-sushi-rolls.jpg",
        "intro": "Seasoned sushi rice and tuna rolled into maki or piled into a bowl ] both come together from the same prep and are ready to eat straight from the fridge all week.",
        "base": {
            "title": "Tuna Sushi Rice",
            "ingredients": [
                "2 cups sushi rice (short-grain Japanese rice)",
                "2½ cups water",
                "3 tbsp rice wine vinegar",
                "1 tbsp sugar",
                "1 tsp fine salt",
                "[ For rolls ]",
                "4 sheets nori",
                "1 can (185g) tuna in spring water, well drained",
                "1 tbsp Japanese mayo",
                "1 Lebanese cucumber, cut into thin batons",
                "Soy sauce, pickled ginger, and wasabi to serve",
                "[ For bowl ]",
                "1 avocado, sliced",
                "1 cucumber, sliced",
                "1 tbsp soy sauce",
                "1 tsp sesame oil",
                "1 tsp sesame seeds",
                "Pickled ginger and nori strips",
            ],
            "steps": [
                "Rinse rice under cold water until water runs clear. Cook with 2½ cups water [ bring to boil, cover, simmer 12 minutes. Remove from heat, rest 10 minutes.",
                "Mix rice vinegar, sugar, and salt until dissolved. Fold through hot rice with a spatula using cutting motions ] fan the rice as you fold to cool it quickly. Do not refrigerate.",
                "Mix drained tuna with Japanese mayo.",
                "Store sushi rice covered with a damp cloth at room temperature [ do not refrigerate or it will harden.",
            ],
        },
        "uses": [
            {
                "name": "Tuna Maki Rolls",
                "image": "",
                "subtitle": "sushi rice and tuna rolled in nori with cucumber",
                "extras": [
                    "Soy sauce, for dipping",
                    "Pickled ginger",
                    "Wasabi",
                ],
                "steps": [
                    "Place a nori sheet shiny-side down on a bamboo mat (or a clean tea towel).",
                    "Spread a thin, even layer of sushi rice over ¾ of the nori, leaving a 2cm border at the far edge.",
                    "Lay a line of tuna mayo and cucumber batons along the near edge.",
                    "Roll firmly away from you, pressing gently as you go. Wet the border to seal.",
                    "Slice into 6–8 pieces with a wet sharp knife. Serve with soy, pickled ginger, and wasabi.",
                ],
                "tip": "Wet your knife between each cut ] it prevents the rice from sticking and gives cleaner slices.",
            },
            {
                "name": "Tuna Rice Bowl",
                "image": "",
                "subtitle": "deconstructed sushi bowl with avocado, cucumber, and sesame",
                "extras": [
                    "1 avocado, sliced",
                    "1 cucumber, thinly sliced",
                    "1 tbsp soy sauce",
                    "1 tsp sesame oil",
                    "Sesame seeds",
                    "Nori strips and pickled ginger",
                ],
                "steps": [
                    "Scoop sushi rice into a bowl.",
                    "Top with tuna mayo, avocado, and cucumber arranged in sections.",
                    "Drizzle soy sauce and sesame oil over everything.",
                    "Finish with sesame seeds, nori strips, and pickled ginger.",
                ],
                "tip": "Add a drizzle of Japanese mayo and a pinch of furikake seasoning if you have it.",
            },
        ],
    },
    {
        "_id": "teriyaki-salmon-udon",
        "_keywords": ["teriyaki", "salmon", "udon", "japanese", "noodles"],
        "image": "/static/images/teriyaki-salmon.jpg",
        "intro": "Salmon fillets lacquered in a sweet soy-mirin teriyaki glaze, served over udon noodles with snap peas, edamame, and pickled ginger - a weeknight dinner that looks like it took effort.",
        "base": {
            "title": "Teriyaki Salmon Udon",
            "ingredients": [
                "4 salmon fillets (about 180g each), skin on",
                "[ Teriyaki glaze ]",
                "3 tbsp soy sauce",
                "3 tbsp mirin",
                "1 tbsp sake (or dry sherry)",
                "1 tbsp sugar",
                "1 tsp sesame oil",
                "[ Noodles ]",
                "400g fresh udon noodles (or 2 dried portions)",
                "1 cup frozen edamame, thawed",
                "1 cup snap peas",
                "2 spring onions, thinly sliced",
                "1 tbsp sesame seeds",
                "Pickled ginger, to serve",
            ],
            "steps": [
                "Combine soy sauce, mirin, sake, and sugar in a small saucepan over medium heat. Stir until sugar dissolves and simmer 3–4 minutes until slightly thickened. Stir in sesame oil and set aside.",
                "Pat salmon dry. Heat a non-stick frying pan over medium-high heat with a little oil. Place salmon skin-side up and cook 3 minutes. Flip and cook 2 minutes more.",
                "Brush generously with teriyaki glaze. Cook 1–2 minutes, brushing again, until the glaze is sticky and caramelised. Remove from heat.",
                "Cook udon noodles per packet instructions. In the last minute, add snap peas to the boiling water. Drain and toss with a splash of soy and sesame oil.",
                "Divide noodles and snap peas into bowls. Add edamame. Place salmon on top and spoon over any remaining glaze.",
                "Finish with spring onions, sesame seeds, and pickled ginger.",
            ],
        },
        "uses": [
            {
                "name": "Teriyaki Salmon Udon Bowl",
                "image": "",
                "subtitle": "glazed salmon over udon with snap peas and pickled ginger",
                "extras": [
                    "Extra teriyaki glaze for drizzling",
                    "Pickled ginger",
                    "Spring onions and sesame seeds",
                ],
                "steps": [
                    "Follow the base recipe through step 6.",
                    "Drizzle any remaining glaze over the salmon before serving.",
                ],
                "tip": "Make a double batch of the teriyaki glaze [ it keeps in the fridge for 2 weeks and works on chicken, tofu, and vegetables too.",
            },
            {
                "name": "Teriyaki Salmon Rice Bowl",
                "image": "",
                "subtitle": "swap noodles for steamed rice",
                "extras": [
                    "2 cups cooked short-grain rice",
                    "Edamame",
                    "Sliced avocado",
                    "Sesame seeds and nori strips",
                ],
                "steps": [
                    "Scoop steamed rice into bowls.",
                    "Top with teriyaki salmon, edamame, and avocado.",
                    "Drizzle with teriyaki glaze and finish with sesame seeds and nori.",
                ],
                "tip": "Flake the salmon into the rice rather than serving it whole ] it distributes the glaze more evenly.",
            },
        ],
    },
    {
        "_id": "beef-fajitas",
        "_keywords": ["beef", "fajita", "mexican", "steak", "peppers"],
        "image": "/static/images/Beef-Fajitas.jpg",
        "intro": "Taco-seasoned stir-fry beef with sautéed bell peppers and red onion - cook it once and build fajita bowls, tacos, or a fajita salad across the week.",
        "base": {
            "title": "Beef Fajitas",
            "ingredients": [
                "1 lb grass-fed stir-fry beef (or skirt/flank steak, sliced thin)",
                "1 red bell pepper, sliced into thin strips",
                "1 yellow bell pepper, sliced into thin strips",
                "1 red onion, sliced into thin strips",
                "2 cloves garlic, minced",
                "2 tbsp taco seasoning",
                "1 tbsp oil",
                "1 lime, cut into wedges",
                "[ Quick guacamole ]",
                "2 avocados, mashed",
                "1 tomato, finely chopped",
                "1/2 lime, juiced",
                "1/8 tsp garlic powder",
                "Salt to taste",
            ],
            "steps": [
                "Slice bell peppers and red onion into thin strips. Mince garlic.",
                "Heat oil in a large sauté pan over medium-high heat. Add garlic, peppers, and onion; sauté about 10 minutes until softened and lightly charred.",
                "Add beef and taco seasoning. Cook about 5 minutes, tossing frequently, until beef is cooked through.",
                "Make the guacamole: mash avocados and stir in chopped tomato, lime juice, garlic powder, and salt to taste.",
                "Store beef and vegetables together; guacamole separately with plastic wrap pressed on the surface to prevent browning.",
            ],
        },
        "uses": [
            {
                "name": "Fajita Tacos",
                "image": "",
                "subtitle": "in warm corn tortillas with guacamole and lime",
                "extras": [
                    "8 corn or flour tortillas",
                    "Guacamole",
                    "Lime wedges",
                    "Sour cream (optional)",
                ],
                "steps": [
                    "Warm tortillas in a dry pan over medium heat or directly over a gas flame.",
                    "Pile beef and peppers into each tortilla.",
                    "Top with a spoonful of guacamole and a squeeze of lime.",
                ],
                "tip": "Char the tortillas slightly for authentic fajita flavor.",
            },
            {
                "name": "Fajita Bowl",
                "image": "",
                "subtitle": "over cilantro rice with guacamole and salsa",
                "extras": [
                    "1 cup cooked rice",
                    "Guacamole",
                    "Salsa",
                    "Shredded cheddar",
                    "Sour cream",
                ],
                "steps": [
                    "Scoop rice into a bowl.",
                    "Top with beef and pepper mixture, a dollop of guacamole, salsa, and cheddar.",
                    "Finish with sour cream and a squeeze of lime.",
                ],
                "tip": "Swap rice for cauliflower rice or quinoa to keep it lighter.",
            },
            {
                "name": "Fajita Salad",
                "image": "",
                "subtitle": "over romaine with guacamole and a lime vinaigrette",
                "extras": [
                    "3 cups romaine, chopped",
                    "Cherry tomatoes, halved",
                    "Guacamole",
                    "2 tbsp olive oil + juice of 1 lime (dressing)",
                    "Tortilla strips (optional)",
                ],
                "steps": [
                    "Toss romaine and tomatoes with olive oil, lime juice, salt, and pepper.",
                    "Top with warm beef and peppers and a spoonful of guacamole.",
                    "Add tortilla strips for crunch.",
                ],
                "tip": "The warm beef wilts the lettuce slightly [ dress and serve immediately.",
            },
        ],
    },
    {
        "_id": "beef-barbacoa",
        "_keywords": ["barbacoa", "beef", "mexican", "chipotle", "shredded beef"],
        "image": "/static/images/Beef-Barbacoa.jpg",
        "intro": "A chipotle-cumin braised chuck roast cooked until fall-apart tender and shredded into its own sauce ] meal prep six portions of bowls or salads in one Instant Pot session.",
        "base": {
            "title": "Beef Barbacoa",
            "ingredients": [
                "2.5 lbs chuck roast",
                "2 tbsp avocado oil, divided",
                "5 garlic cloves, crushed",
                "1/2 tbsp granulated onion",
                "2¼ tbsp ground cumin",
                "1/2 tsp ground cloves",
                "1.5 tbsp dried oregano",
                "2 tsp sea salt",
                "1 tsp black pepper",
                "2 chipotle peppers in adobo, de-seeded and sliced",
                "2 tsp adobo sauce",
                "2 tbsp apple cider vinegar",
                "Juice of 1 lime",
                "1¼ cups beef broth",
                "3 bay leaves",
            ],
            "steps": [
                "Set Instant Pot to sauté. Add 1 tbsp oil and sear the chuck roast 3–5 minutes per side until browned. Remove and set aside.",
                "Add remaining oil and garlic to the pot; sauté 5 minutes. Stir in granulated onion, cumin, cloves, and oregano for 1 minute.",
                "Add adobo sauce, chipotle peppers, lime juice, and vinegar; cook 1 minute.",
                "Pour in beef broth, add bay leaves, salt, and pepper. Return beef to the pot.",
                "Lock lid and pressure cook on HIGH for 70 minutes. Manually release pressure.",
                "Flip the roast, cook on HIGH 5 more minutes, then allow natural pressure release.",
                "Remove bay leaves. Shred beef with two forks and let it sit in the sauce 5 minutes before serving.",
                "Store beef in sauce in the fridge up to 5 days, or freeze up to 3 months.",
            ],
        },
        "uses": [
            {
                "name": "Barbacoa Bowl",
                "image": "",
                "subtitle": "over quinoa with red onion, cilantro, and lime",
                "extras": [
                    "3 cups cooked quinoa (or rice)",
                    "1/4 cup red onion, diced",
                    "1/2 cup fresh cilantro",
                    "Lime wedges",
                    "Avocado or guacamole (optional)",
                ],
                "steps": [
                    "Scoop quinoa into a bowl.",
                    "Top with a generous portion of shredded barbacoa and a spoonful of the braising sauce.",
                    "Add diced red onion, fresh cilantro, and a squeeze of lime.",
                    "Finish with avocado or guacamole if desired.",
                ],
                "tip": "Reheat beef in a skillet over medium heat or microwave in 60-second intervals [ always add a spoonful of the braising sauce to keep it moist.",
            },
            {
                "name": "Barbacoa Salad",
                "image": "",
                "subtitle": "over romaine with chipotle lime dressing",
                "extras": [
                    "3 cups romaine, chopped",
                    "Cherry tomatoes, halved",
                    "1/4 cup red onion, diced",
                    "1/4 cup cilantro",
                    "Lime juice + olive oil (dressing)",
                    "Tortilla strips (optional)",
                ],
                "steps": [
                    "Toss romaine with cherry tomatoes, red onion, and cilantro.",
                    "Dress with olive oil, lime juice, salt, and a pinch of cumin.",
                    "Top with warm shredded barbacoa and tortilla strips for crunch.",
                ],
                "tip": "A spoonful of the braising sauce stirred into the dressing adds smoky depth.",
            },
        ],
    },
    {
        "_id": "breakfast-taco-bowl",
        "_keywords": ["breakfast", "taco", "eggs", "turkey", "potato", "mexican"],
        "image": "/static/images/Taco-Breakfast-Bowl.jpg",
        "intro": "Seasoned ground turkey, scrambled eggs, roasted baby potatoes, fresh pico de gallo, and melted cheese ] all in one bowl. Meal prep four on Sunday and breakfast is handled all week.",
        "base": {
            "title": "Breakfast Taco Bowl",
            "ingredients": [
                "1 lb ground turkey (or ground beef, chicken, or pork)",
                "2 tsp ground cumin",
                "1 tsp chili powder",
                "1 tsp sea salt, divided",
                "8 large eggs",
                "1 lb baby potatoes, halved",
                "1/2 cup shredded Mexican cheese blend",
                "Olive oil or butter",
                "[ Pico de gallo ]",
                "1/2 cup tomatoes, chopped",
                "1/4 cup red onion, minced",
                "2 tbsp cilantro, chopped",
                "1 tbsp lime juice",
                "1/4 tsp salt",
            ],
            "steps": [
                "Roast potatoes: toss halved baby potatoes with olive oil, salt, and pepper. Roast at 400°F for 25–30 minutes until golden.",
                "Cook turkey: heat a skillet over medium heat, add ground turkey and cook 4 minutes breaking it apart. Add cumin, chili powder, and 1/2 tsp salt. Cook 4–5 minutes more until fully cooked.",
                "Scramble eggs: in a separate skillet over low heat, lightly greased, add eggs and cook slowly 5–6 minutes stirring regularly. Season with remaining 1/2 tsp salt.",
                "Make pico: combine tomatoes, red onion, cilantro, lime juice, and salt in a small bowl.",
                "Divide turkey, eggs, and potatoes among 4 containers. Sprinkle cheese over the eggs. Add pico on the side.",
            ],
        },
        "uses": [
            {
                "name": "Taco Bowl",
                "image": "",
                "subtitle": "turkey, eggs, potatoes, pico, and melted cheese",
                "extras": [
                    "Avocado or guacamole",
                    "Sour cream",
                    "Hot sauce",
                ],
                "steps": [
                    "Reheat turkey and potatoes covered in the microwave or oven until warmed through.",
                    "Add scrambled eggs and pico de gallo.",
                    "Top with cheese, avocado, sour cream, and hot sauce.",
                ],
                "tip": "Reheat covered to keep the eggs from drying out [ 60–90 seconds in the microwave works well.",
            },
            {
                "name": "Breakfast Omelet",
                "image": "",
                "subtitle": "filled with seasoned turkey, pico, and cheese",
                "extras": [
                    "2 eggs per omelet",
                    "1 tsp butter",
                    "Shredded Mexican cheese",
                ],
                "steps": [
                    "Whisk 2 eggs with a pinch of salt. Melt butter in a small nonstick pan over medium-low heat.",
                    "Pour in eggs and cook undisturbed until the edges set, about 2 minutes.",
                    "Spoon seasoned turkey and a little pico onto one half. Sprinkle with cheese.",
                    "Fold the omelet over the filling and slide onto a plate. Add avocado or sour cream on the side.",
                ],
                "tip": "Use the pre-cooked turkey straight from the fridge ] the residual heat of the omelet warms it through.",
            },
        ],
    },
    {
        "_id": "wonton-soup",
        "_keywords": ["wonton", "pork", "shrimp", "chinese", "soup", "dumpling"],
        "image": "/static/images/wonton-soup.jpg",
        "intro": "Hand-folded pork and shrimp wontons are the week's MVP [ batch-make them on Sunday and pull from them three different ways: in a clear ginger broth, over rice with chili oil, or floating in a spicy miso ramen.",
        "base": {
            "title": "Pork & Shrimp Wontons",
            "ingredients": [
                "36 wonton wrappers",
                "1 lb ground pork",
                "8 oz medium shrimp, peeled and finely chopped",
                "6 oz napa cabbage, finely chopped",
                "1 tsp salt (for cabbage)",
                "3 green onions, minced",
                "2 cloves garlic, grated",
                "1 tsp ginger, grated",
                "2 tbsp light soy sauce",
                "1 tsp sesame oil",
                "1 tsp Shaoxing wine (or dry sherry)",
                "1/4 tsp white pepper",
                "1/2 tsp sugar",
            ],
            "steps": [
                "Salt chopped napa cabbage; let sit 15 minutes, then squeeze out all water with your hands.",
                "Combine pork, shrimp, squeezed cabbage, green onions, garlic, ginger, soy sauce, sesame oil, Shaoxing wine, white pepper, and sugar. Mix vigorously until the filling becomes sticky.",
                "Set up wrapping station: wrappers, filling, a small bowl of water. Keep unused wrappers under a damp towel.",
                "Place one wrapper as a diamond. Add 1 heaping teaspoon of filling in the center.",
                "Wet the top two edges with water. Fold the bottom point up to the top, creating a triangle. Press to seal firmly.",
                "Wet one bottom corner. Bring both bottom corners together and press to seal. Place on a tray.",
                "Repeat with remaining filling. Wontons keep refrigerated up to 1 day or frozen up to 2 months.",
            ],
        },
        "uses": [
            {
                "name": "Classic Wonton Soup",
                "subtitle": "wontons in clear ginger-sesame broth with chili oil",
                "extras": [
                    "4 cups low-sodium chicken broth",
                    "1 tsp light soy sauce",
                    "1 tsp sesame oil",
                    "1/4 tsp white pepper",
                    "Green onions and chili oil, to finish",
                ],
                "steps": [
                    "Bring broth to a simmer with soy sauce, sesame oil, and white pepper.",
                    "Add 12–15 wontons directly to the simmering broth. Cook 2–3 minutes until wrappers turn semi-transparent and wontons float.",
                    "Ladle into bowls. Finish with sliced green onions and a drizzle of chili oil.",
                ],
                "tip": "Cook wontons directly in the broth ] the starch from the wrappers gives the broth extra body.",
            },
            {
                "name": "Wonton Rice Bowl",
                "subtitle": "wontons over steamed rice with soy-chili sauce",
                "extras": [
                    "2 cups jasmine rice, steamed",
                    "2 tbsp light soy sauce",
                    "1 tbsp chili oil",
                    "1 tsp sesame oil",
                    "1 tsp rice vinegar",
                    "Toasted sesame seeds and sliced green onions",
                ],
                "steps": [
                    "Boil wontons in salted water 2–3 minutes until they float. Drain.",
                    "Whisk together soy sauce, chili oil, sesame oil, and rice vinegar for the sauce.",
                    "Spoon rice into bowls, place wontons on top, and drizzle with sauce. Garnish with sesame seeds and green onions.",
                ],
                "tip": "The chili oil sauce also works cold [ this bowl is great packed for lunch the next day.",
            },
            {
                "name": "Ramen Wonton Soup",
                "subtitle": "wontons in spicy miso ramen broth with bok choy and soft egg",
                "extras": [
                    "2 portions ramen noodles",
                    "4 cups chicken broth",
                    "2 tbsp white miso",
                    "1 tbsp light soy sauce",
                    "2 tbsp chili oil",
                    "2 baby bok choy, halved",
                    "2 soft-boiled eggs, halved",
                    "Sesame seeds and green onions",
                ],
                "steps": [
                    "Warm broth over medium heat. Whisk in miso, soy sauce, and chili oil until smooth.",
                    "Cook ramen noodles per package; drain and divide into bowls.",
                    "Blanch bok choy in the broth for 1 minute. Cook wontons separately in boiling water 2–3 minutes; drain.",
                    "Ladle miso broth over noodles. Add wontons, bok choy, and soft-boiled egg halves. Finish with sesame seeds and green onions.",
                ],
                "tip": "Soft-boil eggs by simmering 6 minutes, then ice-bath and peel. Marinate overnight in soy sauce + mirin for ramen-shop flavor.",
            },
        ],
    },
    {
        "_id": "dan-dan-noodles",
        "_keywords": ["dan dan", "noodles", "pork", "sichuan", "sesame", "chinese"],
        "image": "/static/images/dan-dan-noodles.jpg",
        "intro": "One pork topping and one silky sesame-chili sauce ] cooked once on Sunday, eaten three completely different ways: tossed hot, chilled as a salad, or spooned over smashed cucumber for a no-noodle version.",
        "base": {
            "title": "Dan Dan Noodles",
            "ingredients": [
                "[ Dan Dan Sauce ]",
                "1/3 cup Chinese sesame paste (or tahini)",
                "1/3 cup light soy sauce",
                "1/4 cup Chinkiang vinegar (or rice vinegar)",
                "4 cloves garlic, minced",
                "2 green onions, minced",
                "2 tbsp honey",
                "1/2 tsp ground Sichuan peppercorns",
                "Chili oil, to taste",
                "[ Pork Topping ]",
                "1 lb ground pork",
                "1 tbsp neutral oil",
                "1 tbsp ginger, minced",
                "2 green onions, chopped",
                "1.5 tbsp fermented black beans, rinsed and chopped",
                "1/2 cup Sui Mi Ya Cai (Sichuan preserved mustard greens)",
                "2 tbsp Shaoxing wine",
                "1/2 tsp sugar",
            ],
            "steps": [
                "Make sauce: Whisk sesame paste and soy sauce until smooth. Add vinegar, garlic, green onion, honey, and Sichuan peppercorns; stir until combined. Store in the fridge up to one week.",
                "Brown pork: Heat oil in a skillet over medium-high. Add pork and cook, stirring, until lightly browned.",
                "Add ginger, green onion, fermented black beans, Sui Mi Ya Cai, Shaoxing wine, and sugar. Cook on medium, breaking pork into small pieces, until liquid evaporates and pork is dark and fragrant, about 8–10 minutes.",
                "Cool and store pork and sauce separately in the fridge up to 4 days.",
            ],
        },
        "uses": [
            {
                "name": "Hot Dan Dan Noodles",
                "subtitle": "spicy sesame noodles with pork, bok choy, and crushed peanuts",
                "extras": [
                    "14 oz thin wheat noodles (or spaghetti)",
                    "2 baby bok choy or large handful of spinach",
                    "1/3 cup roasted peanuts, crushed",
                    "Extra chili oil",
                    "Sliced green onions and Sichuan peppercorns to finish",
                ],
                "steps": [
                    "Cook noodles per package; drain. Blanch greens in the same water; drain.",
                    "Add 1/4 cup sauce and a drizzle of chili oil to each bowl.",
                    "Add noodles, top with pork topping and blanched greens. Garnish with crushed peanuts and green onions.",
                    "Toss vigorously before eating so the sauce coats every strand.",
                ],
                "tip": "Sauce thickens in the fridge [ thin with 1–2 tbsp warm water before using if needed.",
            },
            {
                "name": "Cold Dan Dan Noodle Salad",
                "subtitle": "chilled noodles with cucumber, cabbage, sesame sauce, and peanuts",
                "extras": [
                    "14 oz thin noodles",
                    "1 cucumber, julienned",
                    "1 cup shredded red cabbage",
                    "1/3 cup roasted peanuts, crushed",
                    "Toasted sesame seeds",
                ],
                "steps": [
                    "Cook noodles, then rinse under cold running water until completely chilled.",
                    "Thin the sauce with 2–3 tbsp cold water. Toss noodles with sauce until well coated.",
                    "Top with cold pork, julienned cucumber, shredded cabbage, peanuts, and sesame seeds.",
                    "Serve immediately or refrigerate up to 2 days ] keep sauce separate if making ahead.",
                ],
                "tip": "Great packed for lunch: portion noodles into containers and keep sauce in a small jar on the side.",
            },
            {
                "name": "Cucumber & Pork in Dan Dan Sauce",
                "subtitle": "smashed cucumber with pork topping and dan dan sauce [ no noodles",
                "extras": [
                    "2 English cucumbers",
                    "1/4 tsp salt",
                    "1/4 cup roasted peanuts, roughly crushed",
                    "Toasted sesame seeds",
                    "Extra chili oil",
                ],
                "steps": [
                    "Smash cucumbers firmly with the flat of a knife, then cut into bite-sized chunks. Toss with salt; let sit 5 minutes, then pat dry.",
                    "Arrange cucumber in bowls. Spoon 3–4 tbsp dan dan sauce over the top.",
                    "Add a scoop of the warm pork topping. Finish with crushed peanuts, sesame seeds, and chili oil.",
                ],
                "tip": "The cucumber absorbs the sauce just like noodles ] a great low-carb swap that uses all the same flavors.",
            },
        ],
    },
    {
        "_id": "honey-soy-chicken",
        "_keywords": ["honey", "soy", "chicken", "wings", "chinese", "sesame"],
        "image": "/static/images/honey-soy-chicken.jpg",
        "intro": "A soy-ginger marinade does the work overnight, and three rounds of honey glazing in the oven give you impossibly sticky, golden chicken - three side pairings, one marinade.",
        "base": {
            "title": "Chinese Honey Soy Chicken",
            "ingredients": [
                "2 lbs chicken wings",
                "[ Marinade ]",
                "1/4 cup light soy sauce",
                "1 tbsp Shaoxing wine (or dry sherry)",
                "1/2 tsp dark soy sauce",
                "2 scallions, sliced",
                "1 tbsp ginger, grated",
                "4 cloves garlic, grated",
                "1 tbsp light brown sugar",
                "1/2 tsp salt",
                "1/2 tsp black pepper",
                "[ Glaze ]",
                "4 tbsp honey",
                "1 tsp toasted sesame seeds",
            ],
            "steps": [
                "Whisk all marinade ingredients until sugar dissolves. Add chicken and massage to coat thoroughly.",
                "Marinate at least 2 hours at room temperature or overnight in the fridge, flipping halfway.",
                "Preheat oven to 400°F. Line a baking sheet with parchment and set a wire rack on top.",
                "Drain chicken and arrange on the rack. Bake 15 minutes.",
                "Brush all sides with honey; flip skin-side up. Bake 10 more minutes.",
                "Brush again with honey, sprinkle sesame seeds. Bake 5–10 final minutes until golden and sticky.",
                "Rest 5 minutes before serving.",
            ],
        },
        "uses": [
            {
                "name": "With Steamed Rice",
                "subtitle": "sticky glazed chicken over jasmine rice with scallion dipping sauce",
                "extras": [
                    "2 cups jasmine rice, steamed",
                    "2 tbsp light soy sauce",
                    "1 tsp sesame oil",
                    "1 tsp honey",
                    "Sliced scallions",
                ],
                "steps": [
                    "Steam rice while the chicken bakes.",
                    "Mix soy sauce, sesame oil, and honey for a quick dipping sauce.",
                    "Plate rice in bowls, top with 4–5 wings. Scatter scallions and drizzle with dipping sauce.",
                ],
                "tip": "Make extra dipping sauce [ it doubles as a drizzle over rice or a dip for the green beans.",
            },
            {
                "name": "With Garlic Green Beans",
                "subtitle": "wok-blistered green beans with oyster sauce alongside the chicken",
                "extras": [
                    "1 lb green beans, trimmed",
                    "3 cloves garlic, sliced",
                    "1 tbsp neutral oil",
                    "1 tbsp oyster sauce",
                    "1 tsp soy sauce",
                    "Pinch of sugar",
                ],
                "steps": [
                    "Heat oil in a wok or large skillet over high heat until smoking.",
                    "Add green beans and cook undisturbed 2 minutes until blistered and charred in spots.",
                    "Add garlic, oyster sauce, soy sauce, and sugar. Toss vigorously 1 minute.",
                    "Serve the green beans alongside the glazed chicken.",
                ],
                "tip": "High heat is the key ] if the beans steam instead of blister, your pan isn't hot enough.",
            },
            {
                "name": "With Garlic Bok Choy",
                "subtitle": "seared baby bok choy with garlic butter alongside the chicken",
                "extras": [
                    "4 baby bok choy, halved lengthwise",
                    "3 cloves garlic, minced",
                    "1 tbsp butter or neutral oil",
                    "1 tbsp light soy sauce",
                    "Steamed rice, to serve",
                ],
                "steps": [
                    "Heat butter in a wide pan over medium-high. Add bok choy cut-side down.",
                    "Sear undisturbed 2–3 minutes until golden. Add garlic and soy sauce.",
                    "Flip bok choy and cook 1–2 more minutes until just tender.",
                    "Plate rice, bok choy, and glazed chicken together.",
                ],
                "tip": "Sear the bok choy cut-side down without moving it [ you want a caramelized golden face before flipping.",
            },
        ],
    },
    # ── French ───────────────────────────────────────────────────────────────
    {
        "_id": "cheesy-chicken-orzo",
        "_keywords": ["chicken", "orzo", "tomato", "mozzarella", "basil", "one-pot", "french"],
        "image": "/static/images/cheesy-chicken-orzo.jpg",
        "intro": "One Dutch oven does everything ] chicken thighs braise right in a garlicky tomato sauce while orzo absorbs all the flavor, then a blanket of melted mozzarella and fresh basil finishes it.",
        "base": {
            "title": "One-Pot Cheesy Chicken & Orzo",
            "ingredients": [
                "4 bone-in, skin-on chicken thighs",
                "1 cup orzo",
                "1 can (28 oz) crushed tomatoes",
                "1 cup chicken broth",
                "1 yellow onion, diced",
                "4 cloves garlic, sliced",
                "1 red bell pepper, diced",
                "1/3 cup kalamata olives, pitted",
                "1 tsp dried oregano",
                "1/2 tsp red pepper flakes",
                "Salt and black pepper",
                "2 tbsp olive oil",
                "4 oz fresh mozzarella, torn",
                "Large handful fresh basil, chiffonade",
            ],
            "steps": [
                "Pat chicken dry and season generously with salt, pepper, and oregano.",
                "Heat olive oil in a Dutch oven over medium-high. Sear chicken skin-side down 5–6 minutes until golden. Flip and sear 2 more minutes. Remove and set aside.",
                "Reduce heat to medium. Sauté onion and bell pepper in the same pot 4 minutes. Add garlic and cook 1 minute more.",
                "Add crushed tomatoes, broth, red pepper flakes, and olives. Stir to combine.",
                "Nestle chicken thighs back in, skin-side up. Bring to a simmer, cover, and cook 20 minutes.",
                "Stir in orzo, making sure it's submerged. Cover and cook 10–12 minutes until orzo is tender and has absorbed most of the liquid.",
                "Lay mozzarella over the top, cover 2 minutes to melt. Finish with fresh basil.",
            ],
        },
        "uses": [
            {
                "name": "Cheesy Chicken Orzo Bowl",
                "subtitle": "straight from the pot [ chicken, saucy orzo, melted mozzarella, basil",
                "extras": [
                    "Extra fresh basil",
                    "Drizzle of good olive oil",
                    "Crusty bread for scooping",
                ],
                "steps": [
                    "Serve directly from the Dutch oven in wide bowls.",
                    "Tear a little extra mozzarella on top if you like, drizzle with olive oil.",
                    "Pass bread alongside.",
                ],
                "tip": "The orzo thickens as it sits ] add a splash of broth to loosen when reheating.",
            },
            {
                "name": "Baked Stuffed Peppers",
                "subtitle": "halved peppers filled with the orzo mixture and baked until bubbly",
                "extras": [
                    "3 large bell peppers, halved and seeded",
                    "Extra mozzarella for topping",
                    "Fresh basil to finish",
                ],
                "steps": [
                    "Preheat oven to 375°F. Remove chicken from bones and shred; mix back into the orzo.",
                    "Fill pepper halves with the chicken-orzo mixture. Top each with a slice of mozzarella.",
                    "Bake 25–30 minutes until peppers are tender and cheese is golden.",
                    "Garnish with fresh basil before serving.",
                ],
                "tip": "Pre-roast the peppers 10 minutes before filling for softer results.",
            },
            {
                "name": "Chicken Orzo Soup",
                "subtitle": "thinned into a hearty tomato broth soup with shredded chicken",
                "extras": [
                    "2 cups extra chicken broth",
                    "Shredded Parmesan for serving",
                    "Fresh parsley",
                ],
                "steps": [
                    "Pull chicken from bones and shred. Return to pot with extra broth.",
                    "Bring to a simmer, stirring to loosen the orzo into a soup consistency.",
                    "Ladle into bowls and top with Parmesan and parsley.",
                ],
                "tip": "This reheats beautifully as soup [ the orzo softens even more overnight.",
            },
        ],
    },
    {
        "_id": "salade-nicoise",
        "_keywords": ["tuna", "eggs", "salad", "nicoise", "french", "olives", "cucumber", "lunch"],
        "image": "/static/images/salade-nicoise.jpg",
        "intro": "A classic Provençal composed salad built on a Dijon vinaigrette ] jammy soft-boiled eggs, good-quality tuna, crisp vegetables, and briny olives that you can compose on a platter or toss in a bowl.",
        "base": {
            "title": "Salade Niçoise",
            "ingredients": [
                "4 large eggs",
                "2 cans (5 oz each) oil-packed tuna, drained",
                "1 cup green beans or sugar snap peas, trimmed",
                "1 cup cherry tomatoes, halved",
                "1 English cucumber, sliced",
                "6 radishes, thinly sliced",
                "1/2 cup niçoise or kalamata olives",
                "2 tbsp capers",
                "4 cups mixed greens or butter lettuce",
                "[ Dijon Vinaigrette ]",
                "3 tbsp olive oil",
                "1 tbsp red wine vinegar",
                "1 tsp Dijon mustard",
                "1 small shallot, minced",
                "Salt and black pepper",
            ],
            "steps": [
                "Bring a pot of water to a boil. Gently lower eggs in and cook exactly 7 minutes for jammy yolks. Transfer to an ice bath; peel and halve.",
                "Blanch green beans in the boiling water 2 minutes until bright green. Drain and rinse under cold water.",
                "Whisk all vinaigrette ingredients together until emulsified. Taste and adjust seasoning.",
                "Arrange greens on a large platter. Compose tuna, eggs, green beans, tomatoes, cucumber, radishes, olives, and capers in separate sections.",
                "Drizzle generously with vinaigrette just before serving.",
            ],
        },
        "uses": [
            {
                "name": "Classic Composed Platter",
                "subtitle": "arranged on a platter for sharing [ each component in its own section",
                "extras": [
                    "Extra Dijon vinaigrette on the side",
                    "Crusty baguette slices",
                    "Fresh basil or chives",
                ],
                "steps": [
                    "Spread greens as the base on a wide platter.",
                    "Arrange each component in distinct sections across the greens.",
                    "Drizzle vinaigrette over everything and serve immediately with baguette.",
                ],
                "tip": "Keep all components separate until serving ] the salad holds perfectly prepped in the fridge for 2 days.",
            },
            {
                "name": "Pan Bagnat Sandwich",
                "subtitle": "the classic Niçoise pressed into a crusty roll",
                "extras": [
                    "2 crusty ciabatta rolls or a baguette, split",
                    "Extra tuna and olives",
                    "Sliced hard-boiled egg",
                ],
                "steps": [
                    "Brush cut sides of rolls generously with Dijon vinaigrette.",
                    "Layer tuna, sliced egg, tomatoes, cucumber, olives, and capers.",
                    "Wrap tightly in plastic wrap and press under a heavy pan in the fridge for 30 minutes.",
                    "Slice and serve [ the flavors meld beautifully.",
                ],
                "tip": "Pressing the sandwich is the key ] it lets the bread absorb all the vinaigrette.",
            },
            {
                "name": "Niçoise Grain Bowl",
                "subtitle": "all the same components over warm farro or quinoa",
                "extras": [
                    "1 cup farro or quinoa, cooked",
                    "Extra Dijon vinaigrette",
                    "Lemon wedges",
                ],
                "steps": [
                    "Divide warm farro or quinoa into bowls.",
                    "Arrange tuna, eggs, vegetables, and olives over the grains.",
                    "Drizzle generously with vinaigrette and a squeeze of lemon.",
                ],
                "tip": "Warm grains wilt the greens slightly [ add them last if you prefer them crisp.",
            },
        ],
    },
    {
        "_id": "french-lentil-soup",
        "_keywords": ["lentil", "soup", "french", "chicken", "thyme", "rosemary", "tomato", "dinner"],
        "image": "/static/images/french-lentil-soup.jpg",
        "intro": "Green lentils simmer low and slow with chicken, crushed tomatoes, and a bouquet of Provençal herbs ] deeply savory and even better the next day, three ways to serve it through the week.",
        "base": {
            "title": "French Lentil Soup",
            "ingredients": [
                "1.5 lbs bone-in, skin-on chicken thighs",
                "1.5 cups green or brown lentils, rinsed",
                "1 can (14 oz) crushed tomatoes",
                "6 cups chicken broth",
                "1 yellow onion, diced",
                "3 carrots, diced",
                "3 stalks celery, diced",
                "4 cloves garlic, minced",
                "2 sprigs fresh thyme",
                "1 sprig fresh rosemary",
                "2 bay leaves",
                "1 tsp smoked paprika",
                "1/2 tsp cumin",
                "Salt and black pepper",
                "2 tbsp olive oil",
                "Fresh thyme or parsley, to finish",
            ],
            "steps": [
                "Season chicken thighs with salt, pepper, and smoked paprika.",
                "Heat olive oil in a large Dutch oven over medium-high. Sear chicken skin-side down 5 minutes until golden. Remove and set aside.",
                "In the same pot, sauté onion, carrots, and celery 5 minutes until softened. Add garlic and cumin; cook 1 minute.",
                "Add crushed tomatoes, lentils, broth, thyme, rosemary, and bay leaves. Stir to combine.",
                "Nestle chicken back in. Bring to a boil, then reduce heat and simmer covered 35–40 minutes until lentils are tender.",
                "Remove chicken, pull meat from bones, shred, and return to pot. Discard bones, bay leaves, and herb sprigs.",
                "Adjust seasoning. Ladle into bowls and finish with fresh thyme.",
            ],
        },
        "uses": [
            {
                "name": "Classic Lentil Soup",
                "subtitle": "ladled into bowls with crusty bread and a drizzle of good olive oil",
                "extras": [
                    "Crusty baguette or sourdough",
                    "Drizzle of extra-virgin olive oil",
                    "Fresh thyme leaves",
                    "Cracked black pepper",
                ],
                "steps": [
                    "Ladle soup into wide bowls, making sure to get plenty of chicken and lentils.",
                    "Drizzle with olive oil and scatter fresh thyme over the top.",
                    "Serve with thick slices of crusty bread.",
                ],
                "tip": "The soup thickens overnight [ stir in a splash of broth when reheating.",
            },
            {
                "name": "Lentil Stew over Rice",
                "subtitle": "thick, reduced stew served over steamed white or basmati rice",
                "extras": [
                    "2 cups basmati rice, steamed",
                    "Lemon wedges",
                    "Plain yogurt or crème fraîche",
                ],
                "steps": [
                    "Simmer the soup uncovered an extra 10 minutes to reduce and thicken into a stew.",
                    "Spoon over steamed rice in bowls.",
                    "Add a dollop of yogurt or crème fraîche and a squeeze of lemon.",
                ],
                "tip": "The crème fraîche adds a classic French tang that brightens the rich lentils.",
            },
            {
                "name": "Warm Lentil Salad",
                "subtitle": "cooled lentils tossed with Dijon vinaigrette and served over greens",
                "extras": [
                    "2 tbsp olive oil",
                    "1 tbsp red wine vinegar",
                    "1 tsp Dijon mustard",
                    "2 cups mixed greens",
                    "1 shallot, thinly sliced",
                ],
                "steps": [
                    "Scoop out lentils and chicken (without much broth) and let cool slightly.",
                    "Whisk olive oil, vinegar, and Dijon mustard. Toss with lentils and shallot.",
                    "Serve warm lentil mixture over greens.",
                ],
                "tip": "Save the remaining broth ] it's excellent as a base for another soup or to cook grains in.",
            },
        ],
    },
    {
        "_id": "savory-crepes",
        "_keywords": ["crepes", "ham", "gruyere", "french", "savory", "breakfast", "brunch"],
        "image": "/static/images/savory-crepes.jpg",
        "intro": "A single batch of silky crêpe batter keeps in the fridge all week - take a few minutes each morning to fill them with ham and Gruyère, mushroom béchamel, or smoked salmon.",
        "base": {
            "title": "Savory French Crêpes",
            "ingredients": [
                "[ Crêpe Batter ]",
                "1 cup all-purpose flour",
                "2 large eggs",
                "1 1/4 cups whole milk",
                "2 tbsp unsalted butter, melted, plus more for the pan",
                "1/2 tsp salt",
                "Pinch of nutmeg",
            ],
            "steps": [
                "Blend flour, eggs, milk, melted butter, salt, and nutmeg in a blender until completely smooth.",
                "Rest the batter at least 30 minutes in the fridge (or overnight [ it improves the texture).",
                "Heat a 10-inch nonstick skillet over medium. Brush lightly with butter.",
                "Pour about 1/4 cup batter and swirl immediately to coat the pan in a thin, even layer.",
                "Cook 90 seconds until the edges lift and the bottom is golden. Flip and cook 30 seconds more.",
                "Stack crêpes with parchment between them. Store covered in the fridge up to 4 days.",
            ],
        },
        "uses": [
            {
                "name": "Ham & Gruyère Crêpe",
                "subtitle": "the classic crêpe complète ] ham, melted Gruyère, folded into quarters",
                "extras": [
                    "4 oz thinly sliced ham or prosciutto",
                    "1 cup Gruyère, grated",
                    "1 tbsp Dijon mustard",
                    "Fresh parsley, chopped",
                ],
                "steps": [
                    "Spread a thin layer of Dijon over each crêpe.",
                    "Add 2 slices of ham and a generous handful of Gruyère.",
                    "Fold into quarters and place in a nonstick skillet over medium heat.",
                    "Cook 2–3 minutes per side until the cheese melts and the exterior is crisp.",
                    "Scatter parsley and serve immediately.",
                ],
                "tip": "Press gently with a spatula while it cooks to get even contact and maximum cheese melt.",
            },
            {
                "name": "Mushroom & Béchamel Crêpe",
                "subtitle": "sautéed mushrooms in a quick Gruyère béchamel, folded and crisped",
                "extras": [
                    "8 oz cremini mushrooms, sliced",
                    "1 shallot, minced",
                    "1 tbsp butter",
                    "1 tbsp flour",
                    "3/4 cup whole milk",
                    "1/4 cup Gruyère, grated",
                    "Salt, pepper, fresh thyme",
                ],
                "steps": [
                    "Sauté shallot in butter 2 minutes. Add mushrooms and cook until golden and their liquid evaporates, about 6 minutes.",
                    "Stir in flour and cook 1 minute. Add milk gradually, whisking until smooth. Stir in Gruyère until melted.",
                    "Fill each crêpe with 2–3 spoonfuls of mushroom béchamel. Fold into quarters.",
                    "Crisp in a buttered skillet 2 minutes per side.",
                ],
                "tip": "Let the mushrooms cook completely dry before adding flour or the béchamel will be watery.",
            },
            {
                "name": "Smoked Salmon & Cream Cheese Crêpe",
                "subtitle": "cream cheese and capers, topped with smoked salmon [ no heat needed",
                "extras": [
                    "4 oz smoked salmon",
                    "4 oz cream cheese, softened",
                    "2 tbsp capers",
                    "1 tbsp fresh dill",
                    "Lemon wedges",
                    "Thinly sliced red onion",
                ],
                "steps": [
                    "Spread cream cheese over each crêpe.",
                    "Layer smoked salmon, a few capers, dill, and red onion.",
                    "Roll or fold and serve immediately with a lemon wedge.",
                ],
                "tip": "These are best eaten cold or at room temperature ] no need to reheat.",
            },
        ],
    },
    {
        "_id": "sweet-crepes",
        "_keywords": ["crepes", "sweet", "nutella", "banana", "strawberry", "french", "breakfast", "brunch"],
        "image": "/static/images/sweet-crepes.jpg",
        "intro": "The same silky crêpe batter, slightly sweetened - batch once, then spend five minutes each morning choosing your filling: Nutella and fruit, lemon sugar, or a quick berry compote.",
        "base": {
            "title": "Sweet French Crêpes",
            "ingredients": [
                "[ Crêpe Batter ]",
                "1 cup all-purpose flour",
                "2 large eggs",
                "1 1/4 cups whole milk",
                "2 tbsp unsalted butter, melted, plus more for the pan",
                "1 tbsp sugar",
                "1/2 tsp vanilla extract",
                "Pinch of salt",
            ],
            "steps": [
                "Blend flour, eggs, milk, melted butter, sugar, vanilla, and salt until completely smooth.",
                "Rest the batter at least 30 minutes in the fridge (up to 2 days).",
                "Heat a 10-inch nonstick skillet over medium. Brush lightly with butter.",
                "Pour about 1/4 cup batter and swirl to coat thinly and evenly.",
                "Cook 90 seconds until the edges lift. Flip and cook 30 seconds more.",
                "Stack with parchment between crêpes. Store covered in the fridge up to 4 days.",
            ],
        },
        "uses": [
            {
                "name": "Nutella, Banana & Strawberry",
                "subtitle": "folded crêpes loaded with Nutella, fresh banana, and strawberries",
                "extras": [
                    "4 tbsp Nutella",
                    "1 banana, sliced",
                    "1 cup strawberries, sliced",
                    "Powdered sugar, for dusting",
                    "Chocolate sauce or honey drizzle",
                ],
                "steps": [
                    "Spread a generous tablespoon of Nutella over each warm crêpe.",
                    "Add sliced banana and strawberries to one half. Fold into quarters.",
                    "Arrange on a plate and scatter extra fruit around.",
                    "Dust with powdered sugar and drizzle with extra chocolate sauce or honey.",
                ],
                "tip": "Warm the crêpes in a dry skillet 30 seconds before filling [ the Nutella spreads much easier.",
            },
            {
                "name": "Lemon & Sugar",
                "subtitle": "the simplest French crêpe ] warm crêpe, lemon juice, and crunchy sugar",
                "extras": [
                    "1 lemon, halved",
                    "4 tbsp granulated or caster sugar",
                    "1 tbsp unsalted butter",
                ],
                "steps": [
                    "Melt a little butter in the skillet and rewarm each crêpe.",
                    "Squeeze lemon juice generously over the crêpe.",
                    "Sprinkle a tablespoon of sugar, then fold into quarters.",
                    "Serve immediately while the sugar is still slightly crunchy.",
                ],
                "tip": "Use caster sugar if you have it [ it dissolves faster and gives a cleaner finish.",
            },
            {
                "name": "Berry Compote & Whipped Cream",
                "subtitle": "quick warm berry compote with a dollop of whipped cream",
                "extras": [
                    "1 cup mixed berries (fresh or frozen)",
                    "1 tbsp sugar",
                    "1 tsp lemon juice",
                    "1/2 cup heavy cream, whipped to soft peaks",
                    "Fresh mint, optional",
                ],
                "steps": [
                    "Simmer berries, sugar, and lemon juice in a small saucepan 5 minutes until jammy.",
                    "Spoon compote over warm crêpes and fold loosely.",
                    "Top with a generous dollop of whipped cream and fresh mint.",
                ],
                "tip": "Frozen berries work perfectly here and are often better out of season.",
            },
        ],
    },
    # ── Korean ───────────────────────────────────────────────────────────────
    {
        "_id": "korean-beef-bowls",
        "_keywords": ["korean", "beef", "bulgogi", "rice", "slaw", "sesame", "gochujang", "meal prep"],
        "image": "/static/images/korean-beef-bowls.jpg",
        "intro": "Sticky-sweet bulgogi-style beef grilled until charred, packed over jasmine rice with a crunchy sesame-ginger slaw ] four containers ready to go for the week.",
        "base": {
            "title": "Korean Beef Meal Prep Bowls",
            "ingredients": [
                "1.5 lbs beef (flank steak or thinly sliced ribeye)",
                "[ Marinade ]",
                "3 tbsp soy sauce",
                "1 tbsp sesame oil",
                "1 tbsp brown sugar",
                "1 tbsp gochujang",
                "4 cloves garlic, minced",
                "1 tsp fresh ginger, grated",
                "1 tsp rice vinegar",
                "[ Slaw ]",
                "2 cups shredded purple cabbage",
                "1 cup shredded green cabbage",
                "2 scallions, sliced",
                "1 tbsp sesame oil",
                "1 tbsp rice vinegar",
                "1 tsp soy sauce",
                "1 tsp sesame seeds",
                "[ To serve ]",
                "3 cups jasmine rice, cooked",
                "Sriracha, sesame seeds, cilantro",
            ],
            "steps": [
                "Whisk marinade ingredients. Slice beef thinly against the grain and toss to coat. Marinate 30 minutes minimum (overnight is best).",
                "Make the slaw: toss both cabbages, scallions, sesame oil, rice vinegar, soy sauce, and sesame seeds. Refrigerate.",
                "Heat a grill pan or cast iron over high heat. Cook beef in a single layer 2–3 minutes per side without moving, until caramelized and slightly charred.",
                "Cook jasmine rice according to package directions.",
                "Divide rice, beef, and slaw into 4 meal prep containers. Top with sesame seeds, cilantro, and Sriracha.",
            ],
        },
        "uses": [
            {
                "name": "Classic Beef Bowl",
                "subtitle": "grilled bulgogi beef over rice with sesame slaw and Sriracha",
                "extras": ["Sriracha or gochujang", "Fresh cilantro", "Sesame seeds", "Sliced scallions"],
                "steps": [
                    "Reheat beef and rice in the microwave 90 seconds.",
                    "Top with cold slaw, a drizzle of Sriracha, cilantro, and sesame seeds.",
                ],
                "tip": "Keep the slaw separate until serving so it stays crunchy.",
            },
            {
                "name": "Beef & Rice Lettuce Wraps",
                "subtitle": "the same beef and rice spooned into crisp butter lettuce cups",
                "extras": ["1 head butter lettuce, leaves separated", "Sliced cucumber", "Hoisin or gochujang sauce"],
                "steps": [
                    "Spoon rice and beef into each lettuce cup.",
                    "Add sliced cucumber and a drizzle of hoisin or gochujang.",
                    "Eat immediately [ these don't keep well once assembled.",
                ],
                "tip": "Chill the lettuce leaves in ice water for 10 minutes for extra crunch.",
            },
            {
                "name": "Beef Grain Bowl with Fried Egg",
                "subtitle": "beef and slaw over brown rice topped with a jammy fried egg",
                "extras": ["1 egg per serving", "1 tsp sesame oil", "Brown rice or farro", "Soy sauce to taste"],
                "steps": [
                    "Reheat beef and slaw over brown rice or farro.",
                    "Fry an egg in sesame oil, leaving the yolk runny.",
                    "Lay egg on top and drizzle with soy sauce. Break the yolk and toss everything together.",
                ],
                "tip": "The runny yolk acts as a sauce ] don't skip it.",
            },
        ],
    },
    {
        "_id": "kimchi-fried-rice",
        "_keywords": ["kimchi", "fried rice", "korean", "egg", "sesame", "lunch", "probiotics"],
        "image": "/static/images/kimchi-fried-rice.jpg",
        "intro": "Bold, fermented, and satisfying [ day-old rice stir-fried with kimchi and garlic, crowned with a runny egg. The kimchi does all the seasoning work; everything else takes five minutes.",
        "base": {
            "title": "Kimchi Fried Rice",
            "ingredients": [
                "3 cups day-old cooked rice (cold, straight from the fridge)",
                "1 cup kimchi, chopped, plus 2 tbsp kimchi juice",
                "3 cloves garlic, minced",
                "3 scallions, sliced (whites and greens separated)",
                "1 tbsp soy sauce",
                "1 tbsp gochujang (optional, for more heat)",
                "2 tbsp sesame oil",
                "1 tsp neutral oil",
                "4 large eggs",
                "1 tsp sesame seeds",
                "Nori strips or furikake, optional",
            ],
            "steps": [
                "Heat sesame oil and neutral oil in a large wok or skillet over high heat.",
                "Add scallion whites and garlic. Stir-fry 1 minute until fragrant.",
                "Add chopped kimchi and stir-fry 2–3 minutes until slightly caramelized.",
                "Add cold rice, breaking up any clumps. Press into the pan and let it sit 1 minute to get crispy on the bottom, then toss.",
                "Add soy sauce, kimchi juice, and gochujang if using. Toss well to coat.",
                "Push rice to the side. Fry eggs (or soft-boil separately) to your liking.",
                "Plate rice, lay egg on top. Garnish with scallion greens, sesame seeds, and nori.",
            ],
        },
        "uses": [
            {
                "name": "Classic Kimchi Fried Rice + Egg",
                "subtitle": "fried rice topped with a runny fried egg and sesame seeds",
                "extras": ["1 egg per bowl", "Sesame seeds", "Sliced scallions", "Nori strips"],
                "steps": [
                    "Reheat fried rice in a skillet over medium-high with a splash of sesame oil until sizzling.",
                    "Fry an egg alongside, leaving the yolk runny.",
                    "Plate rice, top with egg, and scatter sesame seeds and scallions.",
                ],
                "tip": "High heat is essential ] a hot pan gives you the crispy bottom that makes this dish.",
            },
            {
                "name": "Kimchi Rice with Crispy Tofu",
                "subtitle": "the fried rice with pan-fried crispy tofu for extra protein",
                "extras": ["8 oz extra-firm tofu, pressed and cubed", "1 tbsp soy sauce", "1 tsp sesame oil", "1 tsp cornstarch"],
                "steps": [
                    "Toss tofu cubes with soy sauce, sesame oil, and cornstarch.",
                    "Pan-fry in a little oil over medium-high until golden and crispy on all sides.",
                    "Reheat kimchi rice and top with crispy tofu and a soft-boiled egg.",
                ],
                "tip": "Press tofu for at least 20 minutes [ the drier it is, the crispier it gets.",
            },
            {
                "name": "Kimchi Rice with Chicken",
                "subtitle": "sliced grilled chicken thighs folded into the fried rice",
                "extras": ["2 chicken thighs, boneless", "1 tbsp soy sauce", "1 tsp sesame oil", "1 tsp gochujang"],
                "steps": [
                    "Marinate chicken in soy sauce, sesame oil, and gochujang 20 minutes.",
                    "Grill or pan-sear over medium-high 5 minutes per side. Rest and slice.",
                    "Reheat kimchi fried rice and top with sliced chicken.",
                ],
                "tip": "Use thighs ] they stay juicy and don't dry out when reheated with the rice.",
            },
        ],
    },
    {
        "_id": "korean-shrimp-bowls",
        "_keywords": ["shrimp", "korean", "gochujang", "stir fry", "rice noodles", "sesame", "dinner"],
        "image": "/static/images/korean-shrimp-bowls.jpg",
        "intro": "Gochujang-marinated shrimp sautéed until just pink, served over jasmine rice or rice noodles with stir-fried bell peppers and snap peas - light, spicy, and ready in 20 minutes.",
        "base": {
            "title": "Korean Gochujang Shrimp Bowls",
            "ingredients": [
                "1.5 lbs large shrimp, peeled and deveined",
                "[ Marinade ]",
                "2 tbsp gochujang",
                "1 tbsp soy sauce",
                "1 tbsp sesame oil",
                "1 tbsp lime juice",
                "3 cloves garlic, minced",
                "1 tsp honey",
                "[ Stir-fry ]",
                "2 bell peppers (any color), sliced",
                "1 cup snap peas, trimmed",
                "1 tbsp neutral oil",
                "2 scallions, sliced",
                "[ To serve ]",
                "3 cups jasmine rice or rice noodles, cooked",
                "Sesame seeds, lime wedges, fresh cilantro",
            ],
            "steps": [
                "Whisk marinade and toss with shrimp. Marinate 15–30 minutes (no longer or acid will start cooking them).",
                "Cook rice or soak rice noodles according to package directions.",
                "Heat oil in a large skillet or wok over high heat. Add bell peppers and snap peas, stir-fry 3 minutes until just tender-crisp.",
                "Push vegetables to the side. Add shrimp in a single layer and cook 1–2 minutes per side until pink and just cooked through.",
                "Toss everything together. Serve over rice or noodles, topped with scallions, sesame seeds, and a squeeze of lime.",
            ],
        },
        "uses": [
            {
                "name": "Shrimp & Rice Bowl",
                "subtitle": "gochujang shrimp and stir-fried vegetables over jasmine rice",
                "extras": ["Lime wedges", "Fresh cilantro", "Sesame seeds", "Extra gochujang on the side"],
                "steps": [
                    "Reheat shrimp and vegetables in a skillet over medium-high 2 minutes.",
                    "Serve over freshly cooked jasmine rice.",
                    "Squeeze lime and scatter cilantro and sesame seeds over the top.",
                ],
                "tip": "Shrimp overcooks fast when reheated [ 2 minutes max in a hot pan.",
            },
            {
                "name": "Shrimp Rice Noodle Bowl",
                "subtitle": "the same shrimp over slippery rice noodles with sesame dressing",
                "extras": ["200g rice noodles", "1 tbsp sesame oil", "1 tbsp soy sauce", "1 tsp rice vinegar", "Sliced cucumber"],
                "steps": [
                    "Cook rice noodles per package, rinse under cold water, toss with sesame oil.",
                    "Top noodles with shrimp, vegetables, and sliced cucumber.",
                    "Drizzle with soy sauce and rice vinegar. Toss to combine.",
                ],
                "tip": "Cold noodles keep better for meal prep ] toss with sesame oil to prevent sticking.",
            },
            {
                "name": "Shrimp Lettuce Cups",
                "subtitle": "shrimp and vegetables spooned into crisp lettuce cups",
                "extras": ["1 head butter lettuce", "Sliced avocado", "Gochujang mayo (mayo + gochujang)", "Lime wedges"],
                "steps": [
                    "Reheat shrimp and vegetables briefly in a skillet.",
                    "Spoon into lettuce cups and top with avocado.",
                    "Drizzle with gochujang mayo and a squeeze of lime.",
                ],
                "tip": "Mix 2 tbsp mayo with 1 tsp gochujang for an easy spicy sauce.",
            },
        ],
    },
    {
        "_id": "korean-chicken-bowls",
        "_keywords": ["korean", "chicken", "gochujang", "sesame", "rice", "meal prep", "bowls"],
        "image": "/static/images/korean-chicken-bowls.jpg",
        "intro": "Gochujang-glazed chicken thighs roasted until sticky and caramelized, sliced over rice with quick-pickled cucumbers and a sesame drizzle - four meal prep boxes done in 35 minutes.",
        "base": {
            "title": "Korean Chicken Meal Prep Bowls",
            "ingredients": [
                "1.5 lbs boneless, skinless chicken thighs",
                "[ Sauce ]",
                "3 tbsp gochujang",
                "2 tbsp soy sauce",
                "1 tbsp sesame oil",
                "1 tbsp honey",
                "1 tbsp rice vinegar",
                "3 cloves garlic, minced",
                "1 tsp fresh ginger, grated",
                "[ Quick Pickled Cucumber ]",
                "1 English cucumber, thinly sliced",
                "2 tbsp rice vinegar",
                "1 tsp sugar",
                "1/2 tsp salt",
                "1/2 tsp sesame oil",
                "[ To serve ]",
                "3 cups jasmine rice, cooked",
                "Sesame seeds, scallions, Sriracha",
            ],
            "steps": [
                "Whisk sauce ingredients. Reserve 2 tbsp for drizzling. Toss chicken with remaining sauce.",
                "Make pickled cucumber: toss sliced cucumber with rice vinegar, sugar, salt, and sesame oil. Refrigerate while chicken cooks.",
                "Heat a skillet or grill pan over medium-high. Cook chicken 5–6 minutes per side until cooked through and nicely caramelized.",
                "Rest 5 minutes, then slice against the grain.",
                "Divide rice into 4 containers. Top with sliced chicken, pickled cucumbers, and a drizzle of reserved sauce.",
                "Finish with sesame seeds and scallions.",
            ],
        },
        "uses": [
            {
                "name": "Korean Chicken Rice Bowl",
                "subtitle": "sliced gochujang chicken over rice with pickled cucumber and sesame",
                "extras": ["Reserved gochujang sauce", "Sesame seeds", "Sliced scallions", "Fried egg (optional)"],
                "steps": [
                    "Reheat chicken and rice in the microwave 90 seconds.",
                    "Add pickled cucumbers cold on top.",
                    "Drizzle with reserved sauce, scatter sesame seeds and scallions.",
                ],
                "tip": "Add a fried egg on top [ the yolk makes an extra sauce.",
            },
            {
                "name": "Chicken Bibimbap-Style",
                "subtitle": "rice topped with chicken, cucumber, spinach, and a fried egg with gochujang",
                "extras": ["2 cups baby spinach, wilted", "1 egg per bowl", "Extra gochujang", "Sesame oil drizzle"],
                "steps": [
                    "Wilt spinach in a hot pan with a splash of sesame oil and a pinch of salt.",
                    "Place rice in bowls. Arrange chicken, cucumber, and spinach in sections.",
                    "Top with a fried egg and a generous spoonful of gochujang. Mix everything at the table.",
                ],
                "tip": "Real bibimbap is all about mixing ] the gochujang should coat every grain of rice.",
            },
            {
                "name": "Chicken & Veggie Stir-Fry",
                "subtitle": "slice the chicken and toss with wok-fried broccoli and snap peas",
                "extras": ["1 head broccoli, cut into florets", "1 cup snap peas", "1 tbsp soy sauce", "1 tsp sesame oil", "Steamed rice"],
                "steps": [
                    "Stir-fry broccoli and snap peas in a hot wok with sesame oil and soy sauce, 3–4 minutes.",
                    "Add sliced pre-cooked chicken and toss to heat through.",
                    "Serve over fresh rice with extra gochujang sauce.",
                ],
                "tip": "The chicken is already cooked, so it just needs to warm up [ 1 minute max in the wok.",
            },
        ],
    },
    {
        "_id": "korean-spicy-tofu",
        "_keywords": ["tofu", "korean", "gochujang", "spicy", "mapo", "dinner", "vegetarian"],
        "image": "/static/images/korean-spicy-tofu.jpg",
        "intro": "Silken tofu cubes braised in a bold gochujang sauce with ground pork, bell peppers, and scallions ] a Korean-style Dubu Jorim that's deeply savory, spicy, and ready in 20 minutes.",
        "base": {
            "title": "Korean Spicy Braised Tofu",
            "ingredients": [
                "2 blocks (14 oz each) firm or extra-firm tofu, cut into 1-inch cubes",
                "4 oz ground pork (or omit for vegetarian)",
                "1 red bell pepper, diced",
                "1 green bell pepper, diced",
                "4 scallions, sliced (whites and greens separated)",
                "4 cloves garlic, minced",
                "1 tsp fresh ginger, grated",
                "[ Sauce ]",
                "3 tbsp gochujang",
                "2 tbsp soy sauce",
                "1 tbsp sesame oil",
                "1 tbsp rice wine or dry sherry",
                "1 tsp sugar",
                "1/2 cup water or vegetable broth",
                "1 tbsp neutral oil",
                "Sesame seeds, to finish",
            ],
            "steps": [
                "Pat tofu dry and cut into cubes. Whisk sauce ingredients together in a small bowl.",
                "Heat neutral oil in a large skillet over medium-high. Add ground pork and cook, breaking up, until browned, about 3 minutes.",
                "Add garlic, ginger, and scallion whites. Stir-fry 1 minute until fragrant.",
                "Add bell peppers and toss 2 minutes.",
                "Pour in the sauce and bring to a simmer. Gently add tofu cubes and fold carefully to coat without breaking.",
                "Simmer 8–10 minutes until sauce thickens and coats everything. Finish with sesame oil.",
                "Serve topped with scallion greens and sesame seeds.",
            ],
        },
        "uses": [
            {
                "name": "Over Steamed Rice",
                "subtitle": "the classic way [ spooned over jasmine rice to soak up the spicy sauce",
                "extras": ["2 cups jasmine rice, steamed", "Extra scallions", "Sesame seeds", "Fried egg (optional)"],
                "steps": [
                    "Serve the braised tofu directly over steamed rice.",
                    "Top with scallion greens and sesame seeds.",
                    "Add a fried egg if you want extra protein ] break the yolk into the sauce.",
                ],
                "tip": "The sauce is the star [ make sure your rice is hot so it absorbs every drop.",
            },
            {
                "name": "Tofu Rice Bowl with Kimchi",
                "subtitle": "spicy tofu bowl with a side of kimchi and crispy garlic",
                "extras": ["1/2 cup kimchi", "2 cloves garlic, thinly sliced", "1 tbsp neutral oil", "Jasmine rice"],
                "steps": [
                    "Fry garlic slices in oil until golden and crispy, about 2 minutes. Drain on paper towels.",
                    "Plate rice, top with tofu and a generous scoop of cold kimchi.",
                    "Scatter crispy garlic on top.",
                ],
                "tip": "Cold kimchi against hot tofu is the contrast that makes this bowl.",
            },
            {
                "name": "Vegetarian Version over Noodles",
                "subtitle": "skip the pork and serve the tofu braised in extra sauce over rice noodles",
                "extras": ["200g rice noodles or udon", "Extra gochujang sauce", "Baby spinach, wilted", "Sesame oil drizzle"],
                "steps": [
                    "Cook noodles per package. Drain and toss with a drizzle of sesame oil.",
                    "Make a double batch of sauce (skip pork, add extra broth). Braise tofu until tender.",
                    "Plate noodles, ladle tofu and sauce over top, add wilted spinach.",
                ],
                "tip": "Udon noodles hold the thick sauce better than rice noodles if you have them.",
            },
        ],
    },
    {
        "_id": "greek-meatball-bowls",
        "_keywords": ["meatball", "greek", "beef", "pita", "hummus", "tzatziki", "meal prep"],
        "image": "/static/images/greek-meatball-bowls.jpg",
        "intro": "Herby beef meatballs over turmeric rice with pita, hummus, tzatziki, cherry tomatoes, and pickled onions ] Greek-style meal prep that reheats beautifully all week.",
        "base": {
            "title": "Greek Meatball Bowls",
            "ingredients": [
                "[ Meatballs ]",
                "2 lbs ground beef (80/20)",
                "1/3 cup breadcrumbs",
                "1 egg",
                "4 cloves garlic, minced",
                "2 tbsp fresh parsley, finely chopped",
                "1 tsp dried oregano",
                "1 tsp cumin",
                "1/2 tsp cinnamon",
                "1 tsp salt, 1/2 tsp pepper",
                "[ Turmeric Rice ]",
                "2 cups long-grain white rice",
                "1 tsp turmeric",
                "1/2 tsp cumin",
                "Salt to taste",
                "[ To Serve ]",
                "1 cup hummus",
                "1 cup tzatziki (store-bought or homemade)",
                "1 pint cherry tomatoes, halved",
                "1 English cucumber, diced",
                "4 pitas, warmed",
                "Quick-pickled red onion",
                "Fresh dill and parsley",
            ],
            "steps": [
                "Preheat oven to 400°F. Mix all meatball ingredients together until just combined [ don't over-mix.",
                "Roll into 1.5-inch balls (about 24 total). Place on a lined baking sheet.",
                "Bake 18–20 minutes until browned and cooked through.",
                "Cook rice with turmeric, cumin, and salt according to package directions.",
                "Quick-pickle red onion: combine thin slices with 1/2 cup warm red wine vinegar, 1 tsp sugar, and pinch of salt. Let sit 20 minutes.",
                "Build bowls: turmeric rice, 4–5 meatballs, a scoop each of hummus and tzatziki, cherry tomatoes, cucumber, and pickled onion. Serve with warm pita.",
            ],
        },
        "uses": [
            {
                "name": "Greek Meatball Bowl",
                "subtitle": "the classic prep ] turmeric rice, hummus, tzatziki, and pita all in one",
                "extras": ["Warm pita", "Extra tzatziki", "Fresh dill", "Lemon wedge"],
                "steps": [
                    "Reheat rice and meatballs.",
                    "Build bowl with rice, meatballs, hummus, tzatziki, tomatoes, and cucumber.",
                    "Finish with fresh dill, a squeeze of lemon, and warm pita on the side.",
                ],
                "tip": "Store the hummus and tzatziki separately so they stay creamy and don't make the rice soggy.",
            },
            {
                "name": "Meatball Pita Wraps",
                "subtitle": "stuff the meatballs into pita with tzatziki and salad",
                "extras": ["Pita pockets", "Shredded romaine", "Sliced red onion", "Tzatziki", "Feta crumbles"],
                "steps": [
                    "Warm pita in a dry skillet or oven.",
                    "Slice or halve meatballs and tuck inside pita.",
                    "Add romaine, red onion, feta, and a generous drizzle of tzatziki.",
                ],
                "tip": "Crushing the meatballs slightly inside the pita means every bite has meatball.",
            },
            {
                "name": "Greek Meatball Salad",
                "subtitle": "serve meatballs over a big chopped salad instead of rice",
                "extras": ["4 cups romaine, chopped", "Kalamata olives, halved", "Feta crumbles", "Red wine vinaigrette", "Pita chips"],
                "steps": [
                    "Toss romaine, cherry tomatoes, cucumber, olives, and pickled onion with red wine vinaigrette.",
                    "Top with warm meatballs and crumbled feta.",
                    "Scatter pita chips over the top for crunch.",
                ],
                "tip": "Cold leftover meatballs straight from the fridge actually work great here [ no reheating needed.",
            },
        ],
    },
    {
        "_id": "greek-chicken-bowl",
        "_keywords": ["chicken", "greek", "quinoa", "feta", "olives", "cucumber", "salad", "bowl"],
        "image": "/static/images/greek-chicken-bowl.jpg",
        "intro": "Marinated grilled chicken over fluffy quinoa with a bright Greek salad of tomatoes, cucumber, olives, and feta ] a clean, protein-packed bowl you'll want on repeat.",
        "base": {
            "title": "Greek Chicken Bowl",
            "ingredients": [
                "[ Chicken ]",
                "2.5 lbs boneless skinless chicken breast",
                "3 tbsp olive oil",
                "Juice of 1 lemon",
                "4 cloves garlic, minced",
                "1 tsp dried oregano",
                "1 tsp smoked paprika",
                "1/2 tsp cumin",
                "Salt and pepper",
                "[ Quinoa ]",
                "2 cups quinoa, rinsed",
                "4 cups water or chicken broth",
                "Salt",
                "[ Greek Salad ]",
                "1 pint cherry tomatoes, halved",
                "1 English cucumber, diced",
                "1/2 red onion, thinly sliced",
                "1/2 cup kalamata olives, halved",
                "1/2 cup crumbled feta",
                "2 tbsp red wine vinegar",
                "2 tbsp olive oil",
                "1 tsp dried oregano",
            ],
            "steps": [
                "Marinate chicken: whisk olive oil, lemon juice, garlic, oregano, paprika, cumin, salt, and pepper. Coat chicken and refrigerate 30 minutes (or overnight).",
                "Cook quinoa: bring broth to a boil, add quinoa, reduce heat to low, cover and simmer 15 minutes. Fluff with a fork.",
                "Grill or pan-sear chicken over medium-high heat, 5–6 minutes per side, until cooked through (165°F internal). Rest 5 minutes, then slice.",
                "Make Greek salad: toss tomatoes, cucumber, red onion, olives, and feta with red wine vinegar, olive oil, oregano, salt, and pepper.",
                "Build bowls: quinoa base, sliced chicken, generous scoop of Greek salad, and extra feta on top.",
            ],
        },
        "uses": [
            {
                "name": "Classic Greek Chicken Bowl",
                "subtitle": "quinoa, grilled chicken, and Greek salad with extra feta",
                "extras": ["Extra crumbled feta", "Fresh oregano", "Lemon wedge", "Tzatziki dollop"],
                "steps": [
                    "Reheat quinoa and chicken (or enjoy chicken cold).",
                    "Layer quinoa, sliced chicken, and Greek salad.",
                    "Top with extra feta, fresh oregano, a squeeze of lemon, and a spoonful of tzatziki.",
                ],
                "tip": "The salad gets better after a day in the fridge [ the vinegar softens the onion and deepens the flavors.",
            },
            {
                "name": "Greek Chicken Wrap",
                "subtitle": "sliced chicken and salad rolled into a warm lavash or pita",
                "extras": ["Large lavash or pita flatbreads", "Tzatziki", "Baby spinach", "Hummus"],
                "steps": [
                    "Warm lavash in a dry skillet 30 seconds per side.",
                    "Spread a layer of hummus, then tzatziki over the base.",
                    "Layer spinach, sliced chicken, and a scoop of Greek salad. Roll tightly and slice in half.",
                ],
                "tip": "Wrap in parchment before slicing to hold it together.",
            },
            {
                "name": "Greek Chicken Salad Plate",
                "subtitle": "skip the quinoa and serve over a big bed of romaine",
                "extras": ["4 cups romaine, chopped", "Pita chips or warm pita", "Tzatziki", "Extra kalamata olives"],
                "steps": [
                    "Chop romaine and spread across a large plate or bowl.",
                    "Add sliced chicken, Greek salad, and extra olives.",
                    "Serve tzatziki on the side with pita chips for dipping.",
                ],
                "tip": "This is the version to make when you want something lighter ] all the flavor, no grain.",
            },
        ],
    },
    {
        "_id": "mediterranean-lamb-bowl",
        "_keywords": ["lamb", "ground lamb", "mediterranean", "greek", "hummus", "pita", "turmeric rice", "feta"],
        "image": "/static/images/mediterranean-lamb-bowl.jpg",
        "intro": "Spiced ground lamb over turmeric rice with hummus, feta, cucumber, tomatoes, and warm pita - a rich Mediterranean bowl that comes together in under 30 minutes.",
        "base": {
            "title": "Mediterranean Lamb Bowls",
            "ingredients": [
                "[ Spiced Lamb ]",
                "2 lbs ground lamb",
                "1 medium onion, finely diced",
                "4 cloves garlic, minced",
                "1 tsp cumin",
                "1 tsp coriander",
                "1/2 tsp cinnamon",
                "1/2 tsp smoked paprika",
                "1/4 tsp allspice",
                "Salt and pepper",
                "2 tbsp fresh mint or parsley, chopped",
                "[ Turmeric Rice ]",
                "2 cups long-grain white rice",
                "1 tsp turmeric",
                "1/2 tsp cumin",
                "Salt",
                "[ To Serve ]",
                "1 cup hummus",
                "1/2 cup crumbled feta",
                "1 English cucumber, diced",
                "1 pint cherry tomatoes, halved",
                "4 pitas, warmed",
                "Fresh mint and parsley",
                "Lemon wedges",
            ],
            "steps": [
                "Cook rice with turmeric, cumin, and salt per package directions.",
                "Brown lamb in a large skillet over medium-high heat, breaking it up, 5–6 minutes.",
                "Drain excess fat. Add onion and cook 3 minutes until soft.",
                "Add garlic and all spices. Cook 1–2 minutes until fragrant.",
                "Stir in fresh mint or parsley. Season with salt and pepper.",
                "Build bowls: turmeric rice, spiced lamb, a generous scoop of hummus, cucumber, tomatoes, and crumbled feta. Finish with fresh herbs, lemon, and warm pita.",
            ],
        },
        "uses": [
            {
                "name": "Lamb Rice Bowl",
                "subtitle": "the full bowl [ turmeric rice, lamb, hummus, and all the toppings",
                "extras": ["Tzatziki", "Pickled red onion", "Extra feta", "Lemon wedge", "Fresh mint"],
                "steps": [
                    "Reheat lamb and rice.",
                    "Assemble bowl with rice, lamb, hummus, cucumber, tomatoes, and feta.",
                    "Add pickled onion, tzatziki, fresh mint, and a squeeze of lemon.",
                ],
                "tip": "A drizzle of good olive oil right before serving makes the whole bowl taste restaurant-quality.",
            },
            {
                "name": "Lamb Pita Pockets",
                "subtitle": "stuff the spiced lamb into warm pita with all the fixings",
                "extras": ["Pita pockets", "Shredded iceberg lettuce", "Tzatziki", "Sliced tomatoes", "Feta"],
                "steps": [
                    "Warm pitas until soft and pliable.",
                    "Stuff with warm lamb, shredded lettuce, tomato, and feta.",
                    "Spoon tzatziki generously inside.",
                ],
                "tip": "Line the pita with a smear of hummus before adding the lamb ] it keeps the pita from getting soggy.",
            },
            {
                "name": "Lamb over Orzo Salad",
                "subtitle": "serve the spiced lamb over a lemony orzo and cucumber salad",
                "extras": ["1.5 cups dry orzo", "Juice of 1 lemon", "2 tbsp olive oil", "Fresh parsley", "Kalamata olives", "Feta"],
                "steps": [
                    "Cook orzo per package, drain, and toss with lemon juice, olive oil, salt, and parsley while warm.",
                    "Fold in diced cucumber, halved cherry tomatoes, olives, and feta.",
                    "Serve warm lamb over the orzo salad.",
                ],
                "tip": "The orzo salad is also great cold [ make it ahead and the flavors meld overnight.",
            },
        ],
    },
    {
        "_id": "greek-breakfast-bowl",
        "_keywords": ["greek", "breakfast", "bowl", "eggs", "feta", "cucumber", "olives", "crackers", "cottage cheese", "mediterranean"],
        "image": "/static/images/greek-breakfast-bowl.jpg",
        "intro": "A fresh, protein-packed Mediterranean breakfast bowl with soft-boiled eggs, Greek salad veggies, feta, olives, and crackers ] meal-prepped in 20 minutes and ready all week.",
        "base": {
            "title": "Greek Breakfast Bowl",
            "ingredients": [
                "[ Soft-Boiled Eggs ]",
                "8 large eggs",
                "[ Greek Salad Base ]",
                "1 English cucumber, diced",
                "1 pint cherry tomatoes, halved",
                "1 red bell pepper, diced",
                "1/2 cup kalamata olives, halved",
                "1/2 cup crumbled feta",
                "2 tbsp olive oil",
                "1 tbsp red wine vinegar",
                "1 tsp dried oregano",
                "Salt and pepper",
                "[ To Serve ]",
                "Arugula or baby spinach",
                "Whole grain crackers or pita chips",
                "Fresh parsley, for garnish",
            ],
            "steps": [
                "Bring a pot of water to a boil. Gently lower in eggs and cook 7 minutes for jammy yolks. Transfer immediately to an ice bath for 5 minutes, then peel and halve.",
                "Dice cucumber, halve tomatoes, dice bell pepper. Toss with olive oil, red wine vinegar, oregano, salt, and pepper.",
                "Store Greek salad base in one container, eggs in another, crackers separately.",
                "To assemble: add a handful of arugula to a bowl, top with Greek salad, 2 halved eggs, crumbled feta, olives, and crackers.",
            ],
        },
        "uses": [
            {
                "name": "Classic Greek Breakfast Bowl",
                "subtitle": "soft-boiled eggs, Greek salad, feta, and crackers over arugula",
                "extras": ["Arugula", "Whole grain crackers", "Extra feta", "Lemon wedge"],
                "steps": [
                    "Lay arugula as the base.",
                    "Add the prepped Greek salad, 2 halved soft-boiled eggs, and a scatter of feta and olives.",
                    "Serve with crackers on the side and a squeeze of lemon.",
                ],
                "tip": "Peel eggs the night before and store in water so they're ready to slice in the morning.",
            },
            {
                "name": "Greek Cottage Cheese Bowl",
                "subtitle": "cottage cheese base instead of arugula [ higher protein, creamier texture",
                "extras": ["3/4 cup cottage cheese", "1 tsp olive oil", "Everything bagel seasoning", "Cherry tomatoes", "Cucumber", "Kalamata olives"],
                "steps": [
                    "Spoon cottage cheese into a bowl and drizzle with olive oil.",
                    "Top with cherry tomatoes, cucumber, olives, and crumbled feta.",
                    "Sprinkle with everything bagel seasoning and fresh pepper.",
                    "Add a halved soft-boiled egg on top.",
                ],
                "tip": "Full-fat cottage cheese gives a much creamier result than low-fat ] worth it here.",
            },
            {
                "name": "Open-Face Breakfast Flatbread",
                "subtitle": "spread the Greek salad over warm pita for a lighter handheld version",
                "extras": ["2 pitas or flatbreads", "Tzatziki", "Extra feta", "Fresh dill"],
                "steps": [
                    "Warm pita in the oven at 350°F for 5 minutes until crisp at the edges.",
                    "Spread a thin layer of tzatziki over each pita.",
                    "Top with the Greek salad mix, extra feta, and fresh dill.",
                    "Slice and serve open-face.",
                ],
                "tip": "Add a fried egg on top for extra protein.",
            },
        ],
    },
    {
        "_id": "greek-scrambled-eggs",
        "_keywords": ["eggs", "scrambled", "greek", "feta", "tomatoes", "spinach", "mediterranean", "breakfast"],
        "image": "/static/images/greek-scrambled-eggs.jpg",
        "intro": "Soft, creamy scrambled eggs loaded with cherry tomatoes, wilted spinach, and tangy feta [ a Mediterranean breakfast that comes together in under 10 minutes.",
        "base": {
            "title": "Greek Scrambled Eggs",
            "ingredients": [
                "8 large eggs",
                "2 tbsp milk or cream",
                "1 cup cherry tomatoes, halved",
                "2 cups baby spinach",
                "1/2 cup crumbled feta",
                "2 scallions, sliced",
                "1 tbsp olive oil or butter",
                "1 tsp dried oregano",
                "Salt and pepper",
                "Fresh parsley, to finish",
            ],
            "steps": [
                "Whisk eggs with milk, a pinch of salt, and pepper.",
                "Heat olive oil in a non-stick skillet over medium. Add cherry tomatoes and cook 2 minutes until they start to blister.",
                "Add spinach and stir until just wilted, about 1 minute.",
                "Lower heat to medium-low. Pour in eggs and gently fold with a spatula ] pull from the edges toward the center. Cook until just set but still glossy.",
                "Remove from heat and fold in feta and scallions. Finish with fresh parsley and a pinch of oregano.",
            ],
        },
        "uses": [
            {
                "name": "Classic Plate",
                "subtitle": "straight from the pan with warm toast or pita",
                "extras": ["2 slices sourdough or whole grain bread, toasted", "Extra feta", "Lemon wedge"],
                "steps": [
                    "Toast bread while eggs cook.",
                    "Plate eggs alongside toast with an extra crumble of feta and a squeeze of lemon.",
                ],
                "tip": "Pull the eggs off heat 10 seconds before they look done [ residual heat finishes them perfectly.",
            },
            {
                "name": "Scrambled Egg Pita",
                "subtitle": "stuff the eggs into a warm pita with hummus, cucumber, and tzatziki",
                "extras": ["2 whole wheat pita pockets", "2 tbsp hummus", "1/2 cucumber, sliced", "2 tbsp tzatziki", "Baby spinach or arugula"],
                "steps": [
                    "Warm pita in a dry skillet or directly over a gas flame for 30 seconds per side.",
                    "Spread hummus inside each pita, then a layer of spinach.",
                    "Stuff with warm scrambled eggs, sliced cucumber, and a drizzle of tzatziki.",
                ],
                "tip": "Pack the eggs and pita separately if making ahead ] stuff right before eating so the pita stays crisp.",
            },
            {
                "name": "Over Arugula",
                "subtitle": "serve warm eggs over peppery arugula [ a breakfast salad",
                "extras": ["2 cups arugula", "1 tsp olive oil", "Lemon juice", "Kalamata olives", "Pita chips"],
                "steps": [
                    "Dress arugula with olive oil, lemon juice, and a pinch of salt.",
                    "Spoon warm eggs over the arugula ] the heat wilts the edges slightly.",
                    "Top with olives and pita chips for crunch.",
                ],
                "tip": "The warm eggs act as the dressing here [ don't over-dress the arugula.",
            },
        ],
    },
    {
        "_id": "greek-egg-bites",
        "_keywords": ["eggs", "egg bites", "muffins", "greek", "feta", "spinach", "red pepper", "meal prep", "breakfast"],
        "image": "/static/images/greek-egg-bites.jpg",
        "intro": "Greek-style egg muffins baked in a muffin pan with feta, red peppers, and spinach ] grab two from the fridge every morning and breakfast is done.",
        "base": {
            "title": "Greek Egg Bites",
            "ingredients": [
                "10 large eggs",
                "1/4 cup milk",
                "1 cup baby spinach, roughly chopped",
                "1 red bell pepper, finely diced",
                "1/2 cup crumbled feta",
                "2 scallions, sliced",
                "1 tsp dried oregano",
                "1/2 tsp garlic powder",
                "Salt and pepper",
                "Cooking spray",
            ],
            "steps": [
                "Preheat oven to 350°F. Spray a 12-cup muffin tin generously with cooking spray.",
                "Sauté red pepper in a little olive oil for 3 minutes until slightly softened. Add spinach and cook until just wilted. Let cool slightly.",
                "Whisk eggs with milk, oregano, garlic powder, salt, and pepper.",
                "Divide the spinach and pepper mixture among the 12 muffin cups.",
                "Pour egg mixture over the veggies, filling each cup about 3/4 full.",
                "Top each with crumbled feta and sliced scallions.",
                "Bake 18–22 minutes until eggs are set and slightly golden on top. Cool in the pan 5 minutes before removing.",
            ],
        },
        "uses": [
            {
                "name": "Grab-and-Go",
                "subtitle": "2 egg bites straight from the fridge or quickly reheated",
                "extras": ["1 slice whole grain toast", "Sliced avocado", "Everything bagel seasoning"],
                "steps": [
                    "Microwave 2 egg bites for 30–45 seconds.",
                    "Serve alongside toast with sliced avocado and a sprinkle of everything bagel seasoning.",
                ],
                "tip": "Make a double batch and freeze half [ they reheat from frozen in 60 seconds.",
            },
            {
                "name": "Egg Bite Plate with Greek Salad",
                "subtitle": "3 egg bites with a fresh cucumber-tomato salad and pita",
                "extras": ["1 cup cherry tomatoes, halved", "1/2 cucumber, diced", "Kalamata olives", "Red wine vinaigrette", "Pita or crackers"],
                "steps": [
                    "Toss tomatoes, cucumber, and olives with a drizzle of olive oil, red wine vinegar, salt, and oregano.",
                    "Plate 3 egg bites alongside the salad.",
                    "Serve with pita or crackers.",
                ],
                "tip": "This works as a light lunch too ] just add an extra egg bite.",
            },
            {
                "name": "Egg Bite Breakfast Box",
                "subtitle": "meal-prepped box with egg bites, fresh fruit, and yogurt",
                "extras": ["1/2 cup Greek yogurt", "1/2 cup fresh berries", "Honey drizzle", "A small handful of almonds"],
                "steps": [
                    "Pack 2–3 egg bites in a container.",
                    "Add a small jar of Greek yogurt drizzled with honey, a handful of berries, and almonds.",
                    "Refrigerate [ grab the whole box in the morning.",
                ],
                "tip": "This is a full macro-balanced breakfast (protein, fat, carbs) in one box.",
            },
        ],
    },
    {
        "_id": "shakshuka",
        "_keywords": ["eggs", "shakshuka", "tomato", "mediterranean", "breakfast", "peppers", "spiced", "pita"],
        "image": "/static/images/shakshuka.jpg",
        "intro": "Eggs poached directly in a bold, spiced tomato and pepper sauce ] serve it straight from the skillet with pita bread to scoop up every drop.",
        "base": {
            "title": "Shakshuka",
            "ingredients": [
                "6 large eggs",
                "2 red bell peppers, thinly sliced",
                "1 yellow onion, diced",
                "4 cloves garlic, minced",
                "1 can (28 oz) crushed tomatoes",
                "1 tsp cumin",
                "1 tsp smoked paprika",
                "1/2 tsp chili flakes (or to taste)",
                "1/2 tsp coriander",
                "2 tbsp olive oil",
                "Salt and pepper",
                "Fresh parsley or cilantro, to serve",
                "Pita bread, to serve",
            ],
            "steps": [
                "Heat olive oil in a large oven-safe skillet over medium. Add onion and peppers. Cook, stirring, until softened and starting to caramelize, 8–10 minutes.",
                "Add garlic, cumin, paprika, chili flakes, and coriander. Stir 1 minute until fragrant.",
                "Pour in crushed tomatoes. Season with salt and pepper. Simmer 10 minutes until the sauce thickens and deepens in color.",
                "Use a spoon to make 6 wells in the sauce. Crack one egg into each well.",
                "Cover the skillet and cook 5–8 minutes until whites are set but yolks are still runny.",
                "Scatter fresh parsley over the top. Serve directly from the pan with warm pita.",
            ],
        },
        "uses": [
            {
                "name": "Classic with Pita",
                "subtitle": "straight from the skillet [ eggs in spiced tomato sauce with pita to scoop",
                "extras": ["4 pitas, warmed", "Extra chili flakes", "Fresh parsley or cilantro", "Crumbled feta (optional)"],
                "steps": [
                    "Warm pita in a dry skillet or in the oven.",
                    "Serve shakshuka directly from the pan at the table.",
                    "Crumble feta on top and scatter fresh herbs. Eat with pita to scoop.",
                ],
                "tip": "The sauce keeps well ] make a big batch and just poach fresh eggs in it each morning.",
            },
            {
                "name": "Green Shakshuka",
                "subtitle": "swap the red sauce for a spinach, zucchini, and herb green version",
                "extras": ["2 cups baby spinach", "1 zucchini, grated", "1/2 cup fresh herbs (parsley, cilantro, dill)", "1/2 cup feta", "Greek yogurt dollop"],
                "steps": [
                    "Sauté onion and garlic. Add grated zucchini and cook 3 minutes.",
                    "Add spinach and herbs [ stir until wilted. Season with salt, cumin, and chili flakes.",
                    "Make wells and crack in eggs. Cover and cook until whites are set.",
                    "Top with crumbled feta and a dollop of Greek yogurt.",
                ],
                "tip": "This version is lighter and brighter ] good if you want something less tomato-heavy.",
            },
            {
                "name": "Shakshuka Toast",
                "subtitle": "serve the tomato sauce over thick-cut toast with a poached egg on top",
                "extras": ["2 thick slices sourdough, toasted", "Feta", "Chili flakes", "Fresh basil"],
                "steps": [
                    "Toast sourdough until golden and firm.",
                    "Spoon a generous amount of the shakshuka sauce (without eggs) over each slice.",
                    "Top with a poached or fried egg, crumbled feta, chili flakes, and basil.",
                ],
                "tip": "This is a great way to use leftover sauce [ just make fresh eggs each day.",
            },
        ],
    },
    {
        "_id": "turkish-eggs",
        "_keywords": ["eggs", "turkish", "yogurt", "cilbir", "butter", "chili", "mediterranean", "breakfast"],
        "image": "/static/images/turkish-eggs.jpg",
        "intro": "Silky poached eggs over cool garlicky yogurt, finished with a pool of chili-paprika butter ] Turkish Çılbır that looks impressive but takes about 10 minutes.",
        "base": {
            "title": "Turkish Eggs (Çılbır)",
            "ingredients": [
                "4 large eggs",
                "[ Garlic Yogurt ]",
                "1 cup full-fat Greek yogurt",
                "1 small clove garlic, minced or grated",
                "Juice of 1/2 lemon",
                "Pinch of salt",
                "[ Chili Butter ]",
                "3 tbsp unsalted butter",
                "1 tsp Aleppo pepper or paprika + pinch of chili flakes",
                "1/2 tsp dried mint",
                "[ To Serve ]",
                "Fresh parsley or dill",
                "Crushed walnuts (optional)",
                "Thick toast or crusty bread",
            ],
            "steps": [
                "Mix yogurt with garlic, lemon juice, and salt. Let it come to room temperature (cold yogurt will cool the eggs too fast).",
                "Bring a wide saucepan of water to a gentle simmer. Add a splash of white vinegar. Crack eggs into individual cups.",
                "Create a gentle whirlpool and slide eggs in one at a time. Poach 3 minutes for runny yolks, 4 for jammy. Remove with a slotted spoon and drain.",
                "In a small pan, melt butter over medium heat until foamy. Add Aleppo pepper and mint [ it will sizzle immediately. Remove from heat.",
                "Spoon yogurt onto plates, spreading into a nest. Lay poached eggs on top. Pour chili butter over everything. Finish with parsley and walnuts.",
            ],
        },
        "uses": [
            {
                "name": "Classic Çılbır",
                "subtitle": "poached eggs on garlicky yogurt with chili butter and toast",
                "extras": ["Thick sourdough or rustic bread, toasted", "Extra Aleppo pepper", "Fresh dill"],
                "steps": [
                    "Spread yogurt on the plate. Add poached eggs.",
                    "Pour chili butter over the top and finish with fresh dill and extra Aleppo.",
                    "Serve with toast for dipping into the runny yolk and yogurt.",
                ],
                "tip": "Room-temperature yogurt is the most important detail ] it makes the whole dish taste silkier.",
            },
            {
                "name": "With Soft-Boiled Eggs",
                "subtitle": "swap the poach for soft-boiled [ easier for meal prep",
                "extras": ["4 soft-boiled eggs (7 min)", "Pita chips or crackers", "Fresh parsley", "Sumac"],
                "steps": [
                    "Soft-boil eggs: 7 minutes in boiling water, then ice bath. Peel and halve.",
                    "Spread yogurt on a plate, lay egg halves on top.",
                    "Drizzle with chili butter, scatter parsley and a pinch of sumac.",
                    "Serve with pita chips.",
                ],
                "tip": "Soft-boiled eggs keep in the fridge for 5 days unpeeled ] faster than poaching each morning.",
            },
            {
                "name": "Turkish Egg Bowl",
                "subtitle": "build a heartier bowl with grains, roasted veggies, and the yogurt-egg combo",
                "extras": ["1 cup cooked farro or quinoa", "1 cup roasted cherry tomatoes", "Cucumber", "Crumbled feta"],
                "steps": [
                    "Roast cherry tomatoes at 400°F with olive oil and salt for 20 minutes.",
                    "Build bowl: farro, cucumber, roasted tomatoes, then a generous scoop of garlic yogurt.",
                    "Add poached or soft-boiled eggs, drizzle with chili butter.",
                ],
                "tip": "This turns breakfast into a proper brunch bowl [ great for a lazy weekend morning.",
            },
        ],
    },
    {
        "_id": "ground-turkey-pita",
        "_keywords": ["turkey", "pita", "mediterranean", "greek", "tzatziki", "feta", "lunch", "wrap"],
        "image": "/static/images/ground-turkey-pita.jpg",
        "intro": "Spiced ground turkey seasoned with cumin, oregano, and garlic, stuffed into whole wheat pita with feta, cucumber, and creamy tzatziki ] a quick Mediterranean lunch that meal preps in 20 minutes.",
        "base": {
            "title": "Ground Turkey Pita Pockets",
            "ingredients": [
                "[ Turkey Filling ]",
                "1.5 lbs ground turkey",
                "1/2 yellow onion, finely diced",
                "3 cloves garlic, minced",
                "1 tsp cumin",
                "1 tsp dried oregano",
                "1/2 tsp smoked paprika",
                "1/4 tsp cinnamon",
                "Salt and pepper",
                "1 tbsp olive oil",
                "[ Tzatziki ]",
                "1 cup Greek yogurt",
                "1/2 English cucumber, grated and squeezed dry",
                "1 clove garlic, minced",
                "1 tbsp fresh dill",
                "1 tbsp lemon juice",
                "Salt to taste",
                "[ Assembly ]",
                "4–6 whole wheat pitas",
                "1/2 cup crumbled feta",
                "1 English cucumber, thinly sliced",
                "1 cup cherry tomatoes, halved",
                "2 cups baby spinach or romaine",
            ],
            "steps": [
                "Make tzatziki: grate cucumber, squeeze out excess moisture, and mix with yogurt, garlic, dill, lemon juice, and salt. Refrigerate.",
                "Heat olive oil in a skillet over medium-high. Add onion and cook 3 minutes until soft.",
                "Add turkey and cook, breaking it up, until browned and cooked through, 6–8 minutes.",
                "Add garlic, cumin, oregano, paprika, cinnamon, salt, and pepper. Stir and cook 1–2 more minutes.",
                "Warm pitas in a dry skillet or oven. Open each pita and stuff with spinach, turkey filling, cucumber, cherry tomatoes, and crumbled feta. Finish with a generous spoonful of tzatziki.",
            ],
        },
        "uses": [
            {
                "name": "Classic Pita Pocket",
                "subtitle": "turkey filling with tzatziki, feta, and fresh veggies stuffed in pita",
                "extras": ["Extra tzatziki", "Kalamata olives", "Lemon wedge", "Extra feta"],
                "steps": [
                    "Warm pita and fill with spinach, turkey, cucumber, tomatoes, and feta.",
                    "Drizzle generously with tzatziki. Add olives and a squeeze of lemon.",
                ],
                "tip": "Pack pitas and filling separately for meal prep [ stuff right before eating to keep the pita from getting soggy.",
            },
            {
                "name": "Turkey Pita Bowl",
                "subtitle": "deconstructed ] serve everything over a grain base for a heartier meal",
                "extras": ["1.5 cups cooked quinoa or rice", "Hummus", "Pickled red onion", "Pita chips"],
                "steps": [
                    "Build a bowl with quinoa as the base.",
                    "Add turkey filling, cucumber, tomatoes, and a scoop of hummus.",
                    "Drizzle tzatziki over everything. Top with pickled onion and crumbled feta. Serve with pita chips.",
                ],
                "tip": "The hummus and tzatziki together make the bowl taste extra rich [ don't skip either.",
            },
            {
                "name": "Turkey Flatbread",
                "subtitle": "spread turkey and toppings over a warm flatbread for an open-face version",
                "extras": ["2 large flatbreads or naan", "Hummus", "Baby arugula", "Sliced red onion"],
                "steps": [
                    "Warm flatbread in the oven at 375°F for 5 minutes until crispy at the edges.",
                    "Spread a layer of hummus over each flatbread.",
                    "Top with turkey filling, arugula, sliced red onion, feta, and a drizzle of tzatziki.",
                ],
                "tip": "This version is great for serving a crowd ] slice into pieces and let everyone help themselves.",
            },
        ],
    },
    {
        "_id": "chicken-tandoori-bowls",
        "_keywords": ["chicken", "tandoori", "indian", "meal prep", "yogurt", "rice", "raita", "lunch"],
        "image": "/static/images/chicken-tandoori-bowls.jpg",
        "intro": "Tandoori-marinated chicken thighs roasted until charred and juicy, served over fragrant basmati with cucumber raita and a bright kachumber salad - Indian meal prep that actually gets better after a day in the fridge.",
        "base": {
            "title": "Chicken Tandoori Meal Prep Bowls",
            "ingredients": [
                "[ Tandoori Chicken ]",
                "2.5 lbs boneless skinless chicken thighs",
                "1/2 cup full-fat Greek yogurt",
                "3 tbsp lemon juice",
                "4 cloves garlic, grated",
                "1 tbsp fresh ginger, grated",
                "2 tsp garam masala",
                "1.5 tsp smoked paprika",
                "1 tsp cumin",
                "1 tsp coriander",
                "1/2 tsp turmeric",
                "1/2 tsp chili powder",
                "Salt",
                "[ Cucumber Raita ]",
                "1 cup Greek yogurt",
                "1/2 English cucumber, grated and squeezed",
                "1/2 tsp cumin",
                "1 tbsp fresh cilantro, minced",
                "Salt",
                "[ Kachumber Salad ]",
                "1 cup cherry tomatoes, halved",
                "1 English cucumber, diced",
                "1/2 red onion, finely diced",
                "Juice of 1 lemon",
                "Fresh cilantro",
                "[ Basmati Rice ]",
                "2 cups basmati rice",
            ],
            "steps": [
                "Marinate chicken: mix yogurt, lemon juice, garlic, ginger, and all spices. Coat chicken and marinate at least 2 hours (overnight is best).",
                "Preheat oven to 450°F or set broiler on high. Place chicken on a wire rack over a foil-lined baking sheet.",
                "Roast 20–25 minutes, flipping halfway, until charred at the edges and cooked through (165°F). Rest 5 minutes before slicing.",
                "Cook basmati rice per package directions.",
                "Make raita: mix grated cucumber with yogurt, cumin, cilantro, and salt.",
                "Make kachumber: toss tomatoes, cucumber, and red onion with lemon juice, cilantro, salt, and pepper.",
                "Divide rice into meal prep containers. Top with sliced chicken, kachumber, and a side of raita.",
            ],
        },
        "uses": [
            {
                "name": "Tandoori Chicken Bowl",
                "subtitle": "basmati, sliced chicken, kachumber salad, and cucumber raita",
                "extras": ["Mango chutney", "Fresh cilantro", "Lemon wedge", "Naan (optional)"],
                "steps": [
                    "Reheat rice and chicken.",
                    "Build bowl: rice, sliced chicken, kachumber on one side.",
                    "Spoon raita over chicken and serve with mango chutney and a lemon wedge.",
                ],
                "tip": "The char on the chicken is the flavor [ don't be afraid to let it get dark edges under the broiler.",
            },
            {
                "name": "Chicken Tikka Naan Wrap",
                "subtitle": "sliced tandoori chicken wrapped in warm naan with raita and chutneys",
                "extras": ["2 naans", "Mint chutney", "Sliced red onion", "Baby spinach", "Mango chutney"],
                "steps": [
                    "Warm naan in a skillet or oven.",
                    "Slice chicken and arrange down the center of naan.",
                    "Add baby spinach, red onion, mint chutney, and mango chutney. Roll or fold and eat.",
                ],
                "tip": "Layer both chutneys ] mint for brightness, mango for sweetness. They balance each other.",
            },
            {
                "name": "Tandoori Chicken Salad",
                "subtitle": "serve over a bed of romaine with kachumber and a yogurt-lemon dressing",
                "extras": ["4 cups romaine, chopped", "Chickpeas, drained", "Sliced red onion", "1 tbsp olive oil", "Lemon juice"],
                "steps": [
                    "Toss romaine with olive oil, lemon juice, and salt.",
                    "Top with sliced chicken, kachumber, chickpeas, and red onion.",
                    "Drizzle raita over the top as the dressing.",
                ],
                "tip": "Adding canned chickpeas brings the protein up and makes this more filling without any extra cooking.",
            },
        ],
    },
    {
        "_id": "vegan-indian-curry",
        "_keywords": ["vegan", "indian", "curry", "dal", "lentil", "cauliflower", "kofta", "potato", "coconut", "dinner"],
        "image": "/static/images/vegan-indian-curry.jpg",
        "intro": "A silky coconut red lentil dal served alongside spiced vegetable koftas and basmati rice - a fully plant-based Indian dinner that's rich, warming, and packed with flavor.",
        "base": {
            "title": "Vegan Indian Curry Bowl",
            "ingredients": [
                "[ Red Lentil Dal ]",
                "1.5 cups red lentils, rinsed",
                "1 can (14 oz) coconut milk",
                "1 can (14 oz) crushed tomatoes",
                "2 cups vegetable broth",
                "1 yellow onion, diced",
                "4 cloves garlic, minced",
                "1 tbsp fresh ginger, grated",
                "2 tsp garam masala",
                "1 tsp turmeric",
                "1 tsp cumin",
                "1 tsp coriander",
                "Juice of 1 lemon",
                "Fresh cilantro",
                "[ Vegetable Koftas ]",
                "1 cup cauliflower florets, finely chopped",
                "1 cup potato, boiled and mashed",
                "1/2 cup frozen peas, thawed",
                "1/2 red onion, minced",
                "1 tsp cumin",
                "1 tsp garam masala",
                "1/2 tsp chili powder",
                "3 tbsp chickpea flour (or all-purpose)",
                "Salt and pepper",
                "2 tbsp oil for frying",
                "[ Basmati Rice ]",
                "2 cups basmati rice",
            ],
            "steps": [
                "Make koftas: combine cauliflower, mashed potato, peas, onion, spices, and flour. Mix well [ the mixture should hold together. Form into 1.5-inch balls.",
                "Fry koftas in oil over medium heat, turning, until golden brown on all sides, 6–8 minutes. Set aside.",
                "Make dal: sauté onion in oil until golden, 8 minutes. Add garlic, ginger, and spices ] cook 1 minute.",
                "Add lentils, crushed tomatoes, broth, and coconut milk. Bring to a boil, then simmer 20–25 minutes until lentils are completely soft and dal is thick.",
                "Stir in lemon juice and season with salt. Finish with fresh cilantro.",
                "Cook basmati rice per package directions.",
                "Serve dal alongside koftas and rice, with green chutney if desired.",
            ],
        },
        "uses": [
            {
                "name": "Dal and Kofta Bowl",
                "subtitle": "rice, silky dal, and crispy vegetable koftas with green chutney",
                "extras": ["Green chutney (store-bought or blended cilantro-mint)", "Extra lemon wedge", "Fresh cilantro", "Sliced chili"],
                "steps": [
                    "Reheat dal with a splash of water [ it thickens overnight.",
                    "Warm koftas in the oven at 350°F for 8 minutes or in a skillet.",
                    "Plate rice, ladle dal alongside, and add koftas. Serve with green chutney and lemon.",
                ],
                "tip": "The dal reheats beautifully ] it's actually better on day 2 after the spices meld.",
            },
            {
                "name": "Cauliflower Potato Curry Bowl",
                "subtitle": "skip the dal [ serve just the kofta filling as a dry aloo gobi over rice",
                "extras": ["Extra cauliflower florets, roasted", "Pickled red onion", "Fresh cilantro", "Yogurt dollop (dairy or vegan)"],
                "steps": [
                    "Crumble koftas into a hot skillet and stir-fry with extra cauliflower florets for 3 minutes.",
                    "Season with extra cumin and garam masala.",
                    "Serve over rice with pickled onion, cilantro, and a dollop of yogurt.",
                ],
                "tip": "The crumbled kofta becomes a delicious dry curry that's entirely different from the bowl version.",
            },
            {
                "name": "Dal over Naan",
                "subtitle": "use the lentil dal as a thick sauce spread over toasted naan",
                "extras": ["2 naans", "Fresh cilantro", "Chili flakes", "Lemon wedge", "Plain yogurt or coconut yogurt"],
                "steps": [
                    "Toast naan directly over a gas flame or in a dry skillet until puffed and charred slightly.",
                    "Ladle thick dal over each naan.",
                    "Top with cilantro, a squeeze of lemon, and a drizzle of yogurt.",
                ],
                "tip": "Use thick, slightly re-cooked dal for this ] it should be spreadable, not soupy.",
            },
        ],
    },
    {
        "_id": "paneer-tikka-bowl",
        "_keywords": ["paneer", "tikka", "indian", "rice", "raita", "cucumber", "corn", "marinade", "vegetarian"],
        "image": "/static/images/paneer-tikka-bowl.jpg",
        "intro": "Smoky marinated paneer grilled until golden, served over herbed corn rice with a cool cucumber raita - a stunning Indian vegetarian bowl that's just as satisfying as the chicken version.",
        "base": {
            "title": "Paneer Tikka Rice Bowl",
            "ingredients": [
                "[ Paneer Tikka ]",
                "2 blocks (14 oz each) paneer, cut into 1.5-inch cubes",
                "1 green bell pepper, cut into chunks",
                "1 red bell pepper, cut into chunks",
                "1 red onion, cut into chunks",
                "1 cup full-fat Greek yogurt",
                "2 tbsp lemon juice",
                "3 cloves garlic, grated",
                "1 tbsp fresh ginger, grated",
                "2 tsp garam masala",
                "1 tsp smoked paprika",
                "1 tsp cumin",
                "1/2 tsp turmeric",
                "1/2 tsp chili powder",
                "Salt",
                "[ Herbed Corn Rice ]",
                "2 cups basmati rice",
                "1 cup frozen corn, thawed",
                "3 tbsp fresh cilantro and mint, chopped",
                "1 tbsp butter",
                "Salt",
                "[ Cucumber Raita ]",
                "1 cup Greek yogurt",
                "1/2 English cucumber, diced small",
                "1/2 tsp cumin",
                "Fresh mint and cilantro",
                "Salt",
            ],
            "steps": [
                "Marinate paneer: mix yogurt, lemon juice, garlic, ginger, and all spices. Add paneer and vegetable chunks. Coat well and refrigerate 1–4 hours.",
                "Preheat grill or grill pan to high. Thread paneer, peppers, and onion onto skewers (or cook in batches in a hot skillet).",
                "Grill 3–4 minutes per side until paneer has golden char marks and vegetables are slightly blistered.",
                "Cook basmati rice. While warm, toss with corn, butter, cilantro, and mint.",
                "Make raita: combine yogurt, diced cucumber, cumin, herbs, and salt.",
                "Build bowls: herbed corn rice, paneer tikka skewers, a generous scoop of cucumber raita, and shredded lettuce on the side.",
            ],
        },
        "uses": [
            {
                "name": "Paneer Tikka Bowl",
                "subtitle": "herbed corn rice, grilled paneer, cucumber raita, and fresh salad",
                "extras": ["Shredded romaine or iceberg", "Sliced cucumber", "Fresh mint", "Lemon wedge", "Mango chutney"],
                "steps": [
                    "Reheat rice and paneer (paneer is best reheated in a dry skillet 2 minutes per side to re-char slightly).",
                    "Plate rice, add paneer tikka, fresh romaine and cucumber.",
                    "Spoon raita over paneer. Finish with mango chutney and fresh mint.",
                ],
                "tip": "Re-searing the paneer in a hot dry skillet revives the char [ much better than microwaving.",
            },
            {
                "name": "Paneer Tikka Kebabs",
                "subtitle": "serve the skewers as proper kebabs with mint chutney and pickled onion",
                "extras": ["Mint chutney", "Pickled red onion", "Warm naan or flatbread", "Sliced tomatoes", "Lemon wedge"],
                "steps": [
                    "Grill paneer and vegetables on skewers as in the base recipe.",
                    "Serve skewers directly on warm naan with mint chutney, pickled onion, sliced tomatoes, and a squeeze of lemon.",
                    "Eat as a handheld kebab, pulling pieces off the skewer onto the naan.",
                ],
                "tip": "Char is flavor ] if you have a grill, use it. The smoke makes the paneer taste completely different.",
            },
            {
                "name": "Paneer Tikka Wrap",
                "subtitle": "chop the paneer and wrap everything in a whole wheat lavash",
                "extras": ["2 large whole wheat lavash or tortillas", "Hummus or raita", "Baby spinach", "Shredded cabbage"],
                "steps": [
                    "Warm lavash in a dry skillet.",
                    "Spread a layer of raita down the center.",
                    "Add baby spinach, shredded cabbage, and chopped paneer tikka and vegetables.",
                    "Roll tightly and slice in half.",
                ],
                "tip": "Adding shredded cabbage gives crunch that holds up well even after the wrap sits for a few hours.",
            },
        ],
    },
    {
        "_id": "zucchini-chickpea-curry",
        "_keywords": ["chickpea", "zucchini", "curry", "indian", "vegan", "meal prep", "turmeric", "dinner"],
        "image": "/static/images/zucchini-chickpea-curry.jpg",
        "intro": "Tender chickpeas and zucchini simmered in a bold tomato-spiced curry sauce, served over turmeric rice - a hearty vegan meal prep that keeps beautifully all week.",
        "base": {
            "title": "Zucchini Chickpea Curry",
            "ingredients": [
                "[ Curry ]",
                "2 cans (15 oz each) chickpeas, drained",
                "3 medium zucchini, cut into 1-inch chunks",
                "1 yellow onion, diced",
                "4 cloves garlic, minced",
                "1 tbsp fresh ginger, grated",
                "1 can (14 oz) crushed tomatoes",
                "1 can (14 oz) coconut milk",
                "2 tsp garam masala",
                "1 tsp cumin",
                "1 tsp coriander",
                "1 tsp turmeric",
                "1/2 tsp chili powder",
                "2 tbsp olive oil",
                "Salt and pepper",
                "Fresh cilantro",
                "[ Turmeric Rice ]",
                "2 cups long-grain white rice",
                "1 tsp turmeric",
                "1/2 tsp cumin",
                "Salt",
            ],
            "steps": [
                "Cook turmeric rice: bring water to a boil, add rice, turmeric, cumin, and salt. Simmer until cooked through.",
                "Heat oil in a large pot over medium-high. Add onion and cook 5 minutes until golden.",
                "Add garlic and ginger [ stir 1 minute until fragrant.",
                "Add all spices and stir 30 seconds to bloom them.",
                "Add crushed tomatoes and cook 3 minutes, stirring.",
                "Add chickpeas, zucchini, and coconut milk. Stir to combine. Bring to a simmer.",
                "Cook uncovered 15–18 minutes, stirring occasionally, until zucchini is tender and sauce thickens.",
                "Season with salt and pepper. Finish with fresh cilantro.",
                "Divide turmeric rice and curry into meal prep containers.",
            ],
        },
        "uses": [
            {
                "name": "Meal Prep Bowl",
                "subtitle": "turmeric rice and chickpea curry ready to reheat all week",
                "extras": ["Fresh cilantro", "Lemon wedge", "Yogurt drizzle (optional)"],
                "steps": [
                    "Reheat container in the microwave 2–3 minutes.",
                    "Top with fresh cilantro and a squeeze of lemon.",
                    "Add a drizzle of plain yogurt for a creamy contrast.",
                ],
                "tip": "Store rice and curry in separate compartments so the rice doesn't absorb all the sauce.",
            },
            {
                "name": "Curry with Naan",
                "subtitle": "skip the rice and serve the curry thick with warm naan",
                "extras": ["2–3 naans", "Mango chutney", "Sliced cucumber", "Plain yogurt"],
                "steps": [
                    "Reheat curry and cook down a few extra minutes so it thickens more.",
                    "Warm naan in a dry skillet or oven.",
                    "Serve curry in a bowl alongside naan, yogurt, and mango chutney.",
                ],
                "tip": "A thicker curry works better for naan dipping ] let it simmer an extra 5 minutes uncovered.",
            },
            {
                "name": "Curry Stuffed Baked Potato",
                "subtitle": "spoon the chickpea curry over a fluffy baked potato",
                "extras": ["4 russet potatoes", "Plain Greek yogurt or sour cream", "Sliced scallions", "Chili flakes"],
                "steps": [
                    "Bake potatoes at 400°F for 50–60 minutes until soft. Split open and fluff the interior.",
                    "Reheat curry and spoon generously over each potato.",
                    "Top with Greek yogurt, scallions, and chili flakes.",
                ],
                "tip": "This is a great way to make the curry go further [ one potato per person with a generous scoop of curry is a full meal.",
            },
        ],
    },
    {
        "_id": "coconut-chicken-curry",
        "_keywords": ["chicken", "coconut", "curry", "indian", "spicy", "potato", "peppers", "dinner"],
        "image": "/static/images/coconut-chicken-curry.jpg",
        "intro": "Chicken thighs, golden potatoes, and bell peppers braised in a fragrant coconut curry sauce ] spiced just enough to be exciting, rich enough to feel like a real meal.",
        "base": {
            "title": "Spicy Coconut Chicken Curry",
            "ingredients": [
                "[ Curry ]",
                "2.5 lbs boneless skinless chicken thighs, cut into chunks",
                "2 medium potatoes, peeled and cubed",
                "2 red bell peppers, sliced",
                "1 yellow onion, diced",
                "4 cloves garlic, minced",
                "1 tbsp fresh ginger, grated",
                "1 can (14 oz) coconut milk",
                "1 can (14 oz) crushed tomatoes",
                "2 tsp garam masala",
                "1 tsp cumin",
                "1 tsp coriander",
                "1 tsp turmeric",
                "1/2 tsp chili powder (add more for heat)",
                "1 tbsp fresh thyme leaves",
                "2 tbsp oil",
                "Salt and pepper",
                "Fresh cilantro, to finish",
                "[ Rice ]",
                "2 cups basmati or long-grain white rice",
            ],
            "steps": [
                "Season chicken with salt, pepper, and 1 tsp garam masala.",
                "Heat oil over medium-high. Sear chicken in batches until golden, 4–5 minutes per side. Remove and set aside.",
                "In the same pot, cook onion 5 minutes until soft. Add garlic, ginger, and remaining spices [ stir 1 minute.",
                "Add crushed tomatoes and cook 3 minutes.",
                "Return chicken to pot. Add potatoes, bell peppers, coconut milk, and thyme. Stir.",
                "Bring to a boil, then reduce to a simmer. Cover and cook 25–30 minutes until chicken is tender and potatoes are cooked through.",
                "Uncover and simmer 5 more minutes to thicken the sauce.",
                "Finish with fresh cilantro. Serve over basmati rice.",
            ],
        },
        "uses": [
            {
                "name": "Coconut Chicken Rice Bowl",
                "subtitle": "served over basmati with fresh cilantro and a squeeze of lime",
                "extras": ["Fresh cilantro", "Lime wedge", "Sliced scallions", "Plain yogurt dollop"],
                "steps": [
                    "Reheat curry in a pot with a splash of water, stirring occasionally.",
                    "Serve over fresh basmati rice.",
                    "Finish with cilantro, lime, and a spoonful of plain yogurt to cool the heat.",
                ],
                "tip": "The potatoes absorb the sauce overnight ] day-2 curry is noticeably better.",
            },
            {
                "name": "Coconut Curry with Naan",
                "subtitle": "tear warm naan and use it to scoop the curry",
                "extras": ["2–3 naans", "Extra coconut milk to thin", "Mango chutney", "Sliced red chili"],
                "steps": [
                    "Reheat curry. If it's very thick, stir in a splash of coconut milk.",
                    "Warm naan in the oven or skillet.",
                    "Serve curry in a bowl with naan on the side and mango chutney for dipping.",
                ],
                "tip": "Thin the curry slightly for naan [ it should pour, not scoop.",
            },
            {
                "name": "Curry Chicken Lettuce Cups",
                "subtitle": "serve the curry in butter lettuce cups for a lighter, handheld version",
                "extras": ["8 butter lettuce leaves", "Sliced cucumber", "Fresh mint", "Lime", "Crushed peanuts (optional)"],
                "steps": [
                    "Reheat curry and let cool slightly.",
                    "Lay out lettuce cups on a platter.",
                    "Spoon curry into each cup. Top with cucumber, mint, a squeeze of lime, and crushed peanuts.",
                ],
                "tip": "Shred or chop the chicken before filling the cups ] easier to eat and distribute evenly.",
            },
        ],
    },
    {
        "_id": "coconut-lentil-curry",
        "_keywords": ["lentil", "coconut", "curry", "vegan", "indian", "red lentil", "creamy", "dinner"],
        "image": "/static/images/coconut-lentil-curry.jpg",
        "intro": "Silky red lentils simmered with coconut milk, tomatoes, and warming spices into a deeply comforting curry - vegan, packed with protein, and ready in 30 minutes.",
        "base": {
            "title": "Creamy Coconut Red Lentil Curry",
            "ingredients": [
                "1.5 cups red lentils, rinsed",
                "1 can (14 oz) coconut milk",
                "1 can (14 oz) diced tomatoes",
                "2 cups vegetable broth",
                "1 yellow onion, diced",
                "5 cloves garlic, minced",
                "1 tbsp fresh ginger, grated",
                "2 tsp garam masala",
                "1 tsp cumin",
                "1 tsp turmeric",
                "1 tsp coriander",
                "1/2 tsp chili flakes",
                "2 tbsp olive oil",
                "Juice of 1 lemon",
                "Salt",
                "[ Finish ]",
                "Coconut cream or full-fat coconut milk swirl",
                "Fresh cilantro",
            ],
            "steps": [
                "Heat oil over medium. Add onion and cook 8 minutes until deeply golden.",
                "Add garlic and ginger [ cook 1 minute.",
                "Add all spices and stir 30 seconds to bloom.",
                "Add lentils, diced tomatoes, broth, and coconut milk. Stir well.",
                "Bring to a boil, then reduce to a low simmer. Cook uncovered 20–25 minutes, stirring occasionally, until lentils dissolve into a thick creamy sauce.",
                "Stir in lemon juice and season with salt.",
                "Serve topped with a swirl of coconut cream and fresh cilantro.",
            ],
        },
        "uses": [
            {
                "name": "Over Basmati Rice",
                "subtitle": "the classic way ] creamy lentil curry ladled over fluffy basmati",
                "extras": ["2 cups basmati rice, cooked", "Coconut cream swirl", "Fresh cilantro", "Naan on the side"],
                "steps": [
                    "Reheat lentil curry with a splash of broth or water, stirring until smooth.",
                    "Ladle over basmati rice.",
                    "Swirl coconut cream over the top and finish with cilantro.",
                ],
                "tip": "Red lentil curry thickens dramatically in the fridge [ always add a splash of water when reheating.",
            },
            {
                "name": "Lentil Curry Soup",
                "subtitle": "thin it out with extra broth for a warming, spoonable soup",
                "extras": ["1–2 cups extra vegetable broth", "Crusty bread or naan", "Yogurt or coconut cream", "Chili flakes"],
                "steps": [
                    "Add extra broth to the curry and simmer 5 minutes to create a soup consistency.",
                    "Adjust seasoning ] a thinner curry needs more salt and lemon.",
                    "Serve in bowls with crusty bread and a swirl of coconut cream.",
                ],
                "tip": "A squeeze of extra lemon brightens the soup version [ the spice flavor can get muted when diluted.",
            },
            {
                "name": "Lentil Curry over Roasted Vegetables",
                "subtitle": "spoon thick lentil curry over a sheet pan of roasted cauliflower and sweet potato",
                "extras": ["1 small cauliflower, florets", "1 large sweet potato, cubed", "2 tbsp olive oil", "1 tsp cumin", "Fresh cilantro"],
                "steps": [
                    "Toss cauliflower and sweet potato with olive oil, cumin, salt, and pepper. Roast at 425°F for 25 minutes until golden.",
                    "Reheat curry.",
                    "Arrange roasted vegetables in a bowl and spoon lentil curry over the top.",
                    "Finish with fresh cilantro and a squeeze of lemon.",
                ],
                "tip": "Roasting the vegetables separately keeps them from going mushy ] the contrast of crispy edges against the creamy curry is the point.",
            },
        ],
    },
    {
        "_id": "indian-veggie-rice-bowl",
        "_keywords": ["vegetable", "indian", "curry", "cauliflower", "sweet potato", "broccoli", "rice", "yogurt", "lunch"],
        "image": "/static/images/indian-veggie-rice-bowl.webp",
        "intro": "Indian-spiced roasted vegetables [ cauliflower, sweet potato, and broccoli ] over fluffy rice with a tangy curry yogurt sauce that pulls it all together.",
        "base": {
            "title": "Indian Curry Vegetable Rice Bowls",
            "ingredients": [
                "[ Roasted Vegetables ]",
                "1 small head cauliflower, cut into florets",
                "2 medium sweet potatoes, peeled and cubed",
                "2 cups broccoli florets",
                "3 tbsp olive oil",
                "1.5 tsp curry powder",
                "1 tsp cumin",
                "1/2 tsp turmeric",
                "1/2 tsp smoked paprika",
                "Salt and pepper",
                "[ Curry Yogurt Sauce ]",
                "1 cup plain Greek yogurt",
                "1 tbsp lime juice",
                "1 tsp curry powder",
                "1/2 tsp cumin",
                "1/2 tsp garlic powder",
                "Pinch of chili powder",
                "Salt",
                "[ Rice ]",
                "2 cups basmati or jasmine rice",
                "[ To Finish ]",
                "Fresh cilantro",
                "Lime wedges",
                "Chili flakes",
            ],
            "steps": [
                "Preheat oven to 425°F. Toss cauliflower, sweet potato, and broccoli separately with olive oil and spices (sweet potato needs more time).",
                "Spread sweet potato on one baking sheet, cauliflower and broccoli on another.",
                "Roast sweet potato 30–35 minutes; roast cauliflower and broccoli 22–25 minutes, until edges are caramelized.",
                "Cook rice per package directions.",
                "Make curry yogurt: whisk together yogurt, lime juice, curry powder, cumin, garlic powder, chili powder, and salt.",
                "Build bowls: rice base, roasted vegetables arranged on top, a generous drizzle of curry yogurt sauce, fresh cilantro, and lime.",
            ],
        },
        "uses": [
            {
                "name": "Classic Veggie Bowl",
                "subtitle": "rice, spiced roasted veggies, and curry yogurt drizzle",
                "extras": ["Extra curry yogurt", "Fresh cilantro", "Lime wedge", "Chili flakes", "Pita chips (optional)"],
                "steps": [
                    "Reheat rice and vegetables (oven at 375°F for 8 minutes gives better texture than microwave).",
                    "Arrange over rice and drizzle with curry yogurt.",
                    "Finish with fresh cilantro and a squeeze of lime.",
                ],
                "tip": "Reheat vegetables in the oven so they re-crisp [ microwaving makes them steam and go soggy.",
            },
            {
                "name": "Veggie Bowl with Chickpeas",
                "subtitle": "add a can of roasted chickpeas for extra protein and crunch",
                "extras": ["1 can chickpeas, drained", "1 tsp curry powder", "1 tsp olive oil", "Extra curry yogurt"],
                "steps": [
                    "Toss chickpeas with olive oil and curry powder. Roast at 425°F for 20 minutes until crispy.",
                    "Build the bowl with rice, roasted vegetables, and crispy chickpeas.",
                    "Drizzle with curry yogurt and serve immediately so chickpeas stay crisp.",
                ],
                "tip": "Roasted chickpeas lose their crunch quickly ] add them right before eating, not during meal prep.",
            },
            {
                "name": "Curry Veggie Flatbread",
                "subtitle": "spread curry yogurt over naan and top with roasted veggies",
                "extras": ["2 naans", "Extra curry yogurt", "Arugula", "Pickled red onion", "Feta or paneer crumbles"],
                "steps": [
                    "Warm naan in the oven at 375°F for 5 minutes.",
                    "Spread a thick layer of curry yogurt over each naan.",
                    "Top with warm roasted vegetables, a handful of arugula, pickled onion, and cheese crumbles.",
                ],
                "tip": "This is a great lunch when you want something handheld [ slice the naan in half before serving.",
            },
        ],
    },
    {
        "_id": "indian-savory-toast",
        "_keywords": ["french toast", "savory", "indian", "egg", "masala", "breakfast", "spiced", "casserole"],
        "image": "/static/images/indian-savory-toast.jpg",
        "intro": "Thick-cut bread dipped in a spiced egg mixture with onion, green chili, and cilantro, then pan-fried until golden ] Indian masala French toast that's savory, crispy, and done in 10 minutes.",
        "base": {
            "title": "Indian Savory French Toast",
            "ingredients": [
                "8 thick slices white or sourdough bread",
                "4 large eggs",
                "1/4 cup milk",
                "1/2 small red onion, finely minced",
                "1 green chili or jalapeño, finely minced (seeds removed for less heat)",
                "3 tbsp fresh cilantro, finely chopped",
                "1/2 tsp cumin",
                "1/4 tsp turmeric",
                "1/4 tsp black pepper",
                "Salt to taste",
                "2 tbsp butter or oil for frying",
            ],
            "steps": [
                "Whisk together eggs, milk, onion, green chili, cilantro, cumin, turmeric, pepper, and salt in a wide, shallow bowl.",
                "Heat butter or oil in a skillet over medium.",
                "Dip each bread slice into the egg mixture, pressing gently so it soaks through. Let it sit in the mixture 20–30 seconds per side.",
                "Cook in the skillet 2–3 minutes per side until golden and slightly crispy at the edges.",
                "Serve immediately with mint chutney and ketchup.",
            ],
        },
        "uses": [
            {
                "name": "Classic with Chutneys",
                "subtitle": "straight from the pan with mint chutney and ketchup for dipping",
                "extras": ["Mint chutney (store-bought or blended)", "Ketchup", "Sliced green chili", "Chai"],
                "steps": [
                    "Cook toast in batches, keeping finished pieces warm in a low oven (200°F).",
                    "Serve on a plate with mint chutney and ketchup alongside.",
                    "Eat with chai [ the traditional pairing.",
                ],
                "tip": "Don't press down on the toast while it cooks ] let the crust form naturally for crispier edges.",
            },
            {
                "name": "Savory French Toast Casserole",
                "subtitle": "make a big batch baked in a casserole dish [ perfect for meal prep or brunch",
                "extras": ["1 loaf sourdough, cubed", "8 eggs (double batch of mixture)", "1/2 cup milk", "Extra onion and cilantro", "Cheese (optional ] cheddar or paneer)"],
                "steps": [
                    "Grease a 9×13 baking dish. Cut bread into 1-inch cubes and spread in dish.",
                    "Make a double batch of the egg mixture. Pour over bread and press down gently so all bread soaks.",
                    "Let sit 20 minutes (or refrigerate overnight).",
                    "Bake at 375°F for 30–35 minutes until top is golden and egg is set.",
                    "Slice and serve with mint chutney and ketchup.",
                ],
                "tip": "Overnight soak makes the casserole richer [ assemble the night before for an effortless morning.",
            },
            {
                "name": "Masala Toast Sandwich",
                "subtitle": "make it a sandwich with chutney, cucumber, and cheese in the middle",
                "extras": ["Mint chutney", "Sliced cucumber", "Sliced tomato", "Cheese slice or paneer"],
                "steps": [
                    "Make two slices of masala toast. While still hot, spread mint chutney on one slice.",
                    "Layer with cucumber, tomato, and a slice of cheese or paneer.",
                    "Press together and eat as a sandwich.",
                ],
                "tip": "The hot toast melts the cheese just enough without needing to put it back in the pan.",
            },
        ],
    },
    {
        "_id": "spanish-breakfast-hash",
        "_keywords": ["chorizo", "eggs", "potatoes", "peppers", "spanish", "sheet pan", "hash", "breakfast"],
        "image": "/static/images/spanish-breakfast-hash.jpg",
        "intro": "Crispy sliced potatoes, spiced chorizo, and roasted peppers all on one sheet pan with eggs cracked in at the end ] a bold Spanish breakfast hash that goes from oven to table.",
        "base": {
            "title": "Spanish Sheet Pan Breakfast Hash",
            "ingredients": [
                "6 large eggs",
                "6 oz Spanish chorizo, sliced into rounds",
                "1.5 lbs baby potatoes, thinly sliced",
                "2 red bell peppers, sliced",
                "2 orange or yellow bell peppers, sliced",
                "1 yellow onion, thinly sliced",
                "3 tbsp olive oil",
                "1 tsp smoked paprika",
                "1/2 tsp cumin",
                "Salt and pepper",
                "[ Finish ]",
                "Labneh or Greek yogurt, for drizzling",
                "Fresh parsley or cilantro",
                "Lemon wedges",
                "Chili flakes",
            ],
            "steps": [
                "Preheat oven to 425°F. Line a large rimmed baking sheet with foil and drizzle with olive oil.",
                "Spread sliced potatoes in an even layer. Drizzle with oil, smoked paprika, cumin, salt, and pepper. Toss to coat.",
                "Roast potatoes 20 minutes until starting to brown.",
                "Add chorizo, sliced peppers, and onion to the pan. Toss everything together and spread back out.",
                "Return to oven for 15 more minutes until chorizo is crisp and peppers are soft.",
                "Make 6 wells in the hash and crack an egg into each. Season eggs with salt and pepper.",
                "Bake 6–8 more minutes until whites are set but yolks are still runny.",
                "Drizzle with labneh, scatter fresh parsley, and add lemon wedges and chili flakes.",
            ],
        },
        "uses": [
            {
                "name": "Classic Sheet Pan Hash",
                "subtitle": "everything from the pan [ chorizo, eggs, potatoes, and a labneh drizzle",
                "extras": ["Extra labneh or Greek yogurt", "Lemon wedge", "Chili flakes", "Crusty bread"],
                "steps": [
                    "Serve directly from the sheet pan at the table.",
                    "Drizzle labneh over the top, scatter parsley, and add lemon wedges.",
                    "Eat with crusty bread to mop up the yolk and labneh.",
                ],
                "tip": "Bring the whole sheet pan to the table ] it keeps the hash warm longer and looks dramatic.",
            },
            {
                "name": "Hash in a Warm Tortilla",
                "subtitle": "scoop the hash into flour tortillas for a Spanish-style breakfast wrap",
                "extras": ["4 flour tortillas", "Salsa or hot sauce", "Shredded manchego or cheddar"],
                "steps": [
                    "Warm tortillas in a dry skillet 30 seconds per side.",
                    "Scoop a portion of hash [ chorizo, potato, peppers, and egg ] into each tortilla.",
                    "Top with a drizzle of labneh, a sprinkle of cheese, and hot sauce.",
                ],
                "tip": "This is the best way to use leftover hash the next morning [ reheat in a skillet and wrap.",
            },
            {
                "name": "Hash Over Toast",
                "subtitle": "pile the hash over thick-cut toast for a more contained version",
                "extras": ["4 thick slices sourdough, toasted", "Extra labneh", "Chili flakes", "Fresh herbs"],
                "steps": [
                    "Toast bread until golden and sturdy.",
                    "Spoon a generous portion of hash ] including an egg [ over each slice.",
                    "Drizzle with labneh and finish with chili flakes and fresh herbs.",
                ],
                "tip": "Press the toast down slightly under the hash so it doesn't slide ] it doubles as the base.",
            },
        ],
    },
    {
        "_id": "spanish-scrambled-eggs",
        "_keywords": ["eggs", "scrambled", "spanish", "chickpeas", "vegetables", "peppers", "parmesan", "breakfast"],
        "image": "/static/images/spanish-scrambled-eggs.jpg",
        "intro": "Soft scrambled eggs folded with chickpeas, bell peppers, zucchini, and fresh herbs, finished with grated Parmesan [ a hearty Spanish-style breakfast that's as satisfying as it is easy.",
        "base": {
            "title": "Spanish Scrambled Eggs with Vegetables and Chickpeas",
            "ingredients": [
                "8 large eggs",
                "1 can (15 oz) chickpeas, drained",
                "1 red bell pepper, finely diced",
                "1 green bell pepper, finely diced",
                "1 small zucchini, diced",
                "1/2 yellow onion, finely diced",
                "3 cloves garlic, minced",
                "1/4 cup fresh parsley, chopped",
                "1/4 cup grated Parmesan (plus more to finish)",
                "2 tbsp olive oil",
                "1/2 tsp smoked paprika",
                "Salt and pepper",
                "Crusty bread, to serve",
            ],
            "steps": [
                "Heat olive oil in a large skillet over medium. Add onion and cook 4 minutes until soft.",
                "Add bell peppers and zucchini. Cook 4–5 minutes until slightly softened.",
                "Add garlic and smoked paprika ] stir 1 minute.",
                "Add chickpeas and toss to heat through, 2 minutes.",
                "Whisk eggs with a pinch of salt and pepper. Reduce heat to medium-low.",
                "Pour eggs over the vegetables. Gently fold with a spatula, pulling from the edges inward. Cook until just set but still soft and creamy.",
                "Remove from heat, fold in parsley and Parmesan.",
                "Serve in a warm dish with extra Parmesan grated on top and crusty bread alongside.",
            ],
        },
        "uses": [
            {
                "name": "Classic Plate with Bread",
                "subtitle": "served straight from the pan with crusty bread for scooping",
                "extras": ["Crusty sourdough or baguette", "Extra Parmesan", "Olive oil drizzle", "Fresh parsley"],
                "steps": [
                    "Plate the eggs in a warm serving dish.",
                    "Grate extra Parmesan generously over the top and drizzle with olive oil.",
                    "Serve with thick slices of crusty bread.",
                ],
                "tip": "Cook the eggs low and slow [ they should be creamy and soft, not dry. Pull off heat while they still look slightly underdone.",
            },
            {
                "name": "In a Warm Baguette",
                "subtitle": "stuff the egg mixture into a toasted baguette for a Spanish bocadillo",
                "extras": ["1 baguette, halved and toasted", "Manchego or Parmesan slices", "Sliced tomato", "Olive oil"],
                "steps": [
                    "Toast baguette halves cut-side down in a skillet.",
                    "Rub with a cut garlic clove and drizzle with olive oil.",
                    "Pile egg mixture inside, add sliced tomato and a shaving of manchego.",
                ],
                "tip": "Rubbing the bread with raw garlic while it's still warm is the Spanish way ] you get the flavor without overwhelming it.",
            },
            {
                "name": "Over Rice",
                "subtitle": "serve the egg and chickpea mixture over white rice for a heartier bowl",
                "extras": ["2 cups cooked white rice", "Smoked paprika drizzle", "Olive oil", "Sliced scallions"],
                "steps": [
                    "Serve hot rice in bowls.",
                    "Spoon the egg and chickpea mixture over the top.",
                    "Drizzle with olive oil and a pinch of smoked paprika. Top with scallions.",
                ],
                "tip": "This is the version that travels well for meal prep [ keep eggs and rice separate and combine when reheating.",
            },
        ],
    },
    {
        "_id": "spanish-tortilla",
        "_keywords": ["tortilla", "spanish", "potato", "omelette", "eggs", "breakfast", "meal prep"],
        "image": "",
        "intro": "The classic Spanish omelette ] slowly confited potatoes and onion folded into silky beaten eggs, then cooked into a thick, golden tortilla that slices like a pie and travels perfectly.",
        "base": {
            "title": "Spanish Tortilla (Tortilla Española)",
            "ingredients": [
                "8 large eggs",
                "1.5 lbs Yukon Gold potatoes, peeled and very thinly sliced",
                "1 medium yellow onion, thinly sliced",
                "1 cup olive oil (for confiting)",
                "1.5 tsp salt",
                "Black pepper",
            ],
            "steps": [
                "Heat olive oil in a 10-inch non-stick skillet over medium-low. Add potato slices and onion in layers, seasoning with salt as you go.",
                "Cook slowly, stirring gently occasionally, 20–25 minutes until potatoes are completely tender but not browned. They should almost melt.",
                "Drain potatoes and onion through a colander set over a bowl [ reserve the oil.",
                "Beat eggs well with salt and pepper in a large bowl. Fold in the warm potato-onion mixture. Let sit 5 minutes.",
                "Heat 2 tbsp of the reserved oil in the skillet over medium. Pour in the egg-potato mixture and spread evenly.",
                "Cook 4–5 minutes, shaking the pan gently, until edges are set. The center should still be slightly jiggly.",
                "Place a large plate over the skillet and flip the tortilla onto it in one swift motion. Slide it back into the pan uncooked-side down.",
                "Cook 2–3 more minutes until just set through. Slide onto a plate and rest 5 minutes before slicing.",
            ],
        },
        "uses": [
            {
                "name": "Room Temperature Slices",
                "subtitle": "served at room temp the Spanish way ] with alioli and crusty bread",
                "extras": ["Alioli or garlic mayo", "Crusty bread or baguette", "Sliced tomatoes", "Flaky sea salt"],
                "steps": [
                    "Let the tortilla cool to room temperature [ this is how it's traditionally served in Spain.",
                    "Slice into wedges like a pie.",
                    "Serve with alioli for dipping, sliced tomatoes, and crusty bread.",
                ],
                "tip": "Tortilla is actually better at room temperature than hot ] the texture firms into something custardy and sliceable.",
            },
            {
                "name": "Tortilla Bocadillo",
                "subtitle": "a wedge of tortilla stuffed in a crusty roll [ the classic Spanish sandwich",
                "extras": ["Crusty rolls or baguette, halved", "Alioli or mayonnaise", "Sliced tomato", "Sliced piquillo peppers"],
                "steps": [
                    "Spread alioli on both sides of the roll.",
                    "Tuck a wedge of tortilla inside.",
                    "Add sliced tomato and piquillo peppers.",
                ],
                "tip": "This is the definitive Spanish train station or roadtrip food ] it gets better as it sits.",
            },
            {
                "name": "Tortilla Squares for Meal Prep",
                "subtitle": "slice into squares and pack with salad for a protein-rich lunch",
                "extras": ["Mixed greens or arugula", "Cherry tomatoes", "Red wine vinaigrette", "Extra alioli"],
                "steps": [
                    "Cut tortilla into squares instead of wedges for easier packing.",
                    "Pack with a simple dressed salad of greens, cherry tomatoes, and vinaigrette.",
                    "Include a small container of alioli for dipping.",
                ],
                "tip": "Tortilla keeps in the fridge for 4 days [ make one on Sunday and eat it all week.",
            },
        ],
    },
    {
        "_id": "spanish-tortilla-muffins",
        "_keywords": ["tortilla", "muffins", "spanish", "potato", "eggs", "breakfast", "meal prep", "grab and go"],
        "image": "/static/images/spanish-tortilla-muffins.jpg",
        "intro": "All the flavors of Spanish tortilla baked into individual muffin cups ] crispy potato, caramelized onion, and egg [ with alioli for dipping. Meal prep in 30 minutes, grab 2 every morning.",
        "base": {
            "title": "Spanish Tortilla Egg Muffins",
            "ingredients": [
                "10 large eggs",
                "1 lb baby potatoes or Yukon Gold, diced small",
                "1 medium yellow onion, finely diced",
                "1 tsp smoked paprika",
                "1/2 tsp garlic powder",
                "Salt and pepper",
                "3 tbsp olive oil",
                "Fresh parsley, to garnish",
                "Alioli or garlic mayo, to serve",
                "Cooking spray",
            ],
            "steps": [
                "Preheat oven to 375°F. Spray a 12-cup muffin tin generously.",
                "Sauté diced potato in olive oil over medium heat 8–10 minutes until golden and cooked through. Add onion and cook 4 more minutes until soft.",
                "Season with smoked paprika, garlic powder, salt, and pepper. Let cool slightly.",
                "Whisk eggs with salt and pepper. Divide potato-onion mixture among the 12 muffin cups.",
                "Pour egg mixture over, filling each cup about 3/4 full.",
                "Bake 18–20 minutes until eggs are puffed and set. Cool 5 minutes before removing.",
                "Garnish with fresh parsley. Serve with alioli.",
            ],
        },
        "uses": [
            {
                "name": "Grab-and-Go with Alioli",
                "subtitle": "2 muffins straight from the fridge, reheated, with garlic mayo for dipping",
                "extras": ["Alioli or garlic mayo", "Fresh parsley", "Sliced tomato"],
                "steps": [
                    "Microwave 2 muffins 30–45 seconds.",
                    "Serve with a ramekin of alioli for dipping.",
                ],
                "tip": "Make alioli by mixing mayo with a grated garlic clove, lemon juice, and olive oil ] 2 minutes of work, completely different from plain mayo.",
            },
            {
                "name": "Muffin Plate with Salad",
                "subtitle": "3 muffins plated with a simple Spanish-style tomato salad",
                "extras": ["2 tomatoes, sliced", "Red onion, thin", "Olive oil", "Sherry vinegar", "Fresh basil"],
                "steps": [
                    "Slice tomatoes and red onion. Dress with olive oil, sherry vinegar, salt, and basil.",
                    "Plate 3 muffins alongside the tomato salad.",
                ],
                "tip": "Sherry vinegar instead of red wine vinegar is the Spanish touch that makes the tomato salad taste completely different.",
            },
            {
                "name": "Muffin Sandwich",
                "subtitle": "halve two muffins and stack with alioli, lettuce, and tomato into a slider",
                "extras": ["Slider buns or small rolls", "Alioli", "Butter lettuce", "Sliced tomato", "Piquillo peppers"],
                "steps": [
                    "Slice muffins in half horizontally.",
                    "Spread alioli on both sides of a slider roll.",
                    "Stack with a muffin half, lettuce, tomato, and piquillo peppers.",
                ],
                "tip": "Two muffin halves in a slider is a perfectly portioned breakfast sandwich [ no fork needed.",
            },
        ],
    },
    {
        "_id": "chipotle-rice-bowl",
        "_keywords": ["chicken", "brown rice", "black beans", "grain bowl", "chipotle", "kale", "meal prep", "lunch"],
        "image": "/static/images/chipotle-rice-bowl.jpg",
        "intro": "Seasoned grilled chicken over nutty brown rice with black beans, cherry tomatoes, kale, and a bright cilantro-lime green sauce ] a clean, protein-packed grain bowl that meal preps for the whole week.",
        "base": {
            "title": "Chipotle Brown Rice Grain Bowl",
            "ingredients": [
                "[ Chicken ]",
                "2 lbs boneless skinless chicken breast",
                "1 tbsp olive oil",
                "1 tsp cumin",
                "1 tsp smoked paprika",
                "1/2 tsp garlic powder",
                "Salt and pepper",
                "[ Brown Rice ]",
                "2 cups brown rice",
                "1 tsp cumin",
                "Salt",
                "[ Bowl Components ]",
                "2 cans (15 oz each) black beans, drained and rinsed",
                "1 pint cherry tomatoes, halved",
                "4 cups kale or baby spinach, roughly chopped",
                "1/2 red onion, finely diced",
                "Fresh cilantro",
                "[ Green Sauce ]",
                "1 cup fresh cilantro",
                "1/4 cup olive oil",
                "2 tbsp lime juice",
                "1 clove garlic",
                "1 jalapeño (seeds removed for mild)",
                "Salt",
            ],
            "steps": [
                "Cook brown rice with cumin and salt per package directions.",
                "Season chicken with olive oil, cumin, paprika, garlic powder, salt, and pepper.",
                "Grill or pan-sear chicken over medium-high heat, 5–6 minutes per side, until cooked through. Rest 5 minutes, then slice.",
                "Make green sauce: blend cilantro, olive oil, lime juice, garlic, jalapeño, and salt until smooth.",
                "Warm black beans in a small pot with salt, cumin, and a splash of water.",
                "Build bowls: brown rice base, sliced chicken, black beans, cherry tomatoes, kale, and red onion. Serve green sauce on the side.",
            ],
        },
        "uses": [
            {
                "name": "Classic Grain Bowl",
                "subtitle": "brown rice, chicken, black beans, and green sauce [ the full build",
                "extras": ["Extra green sauce", "Lime wedge", "Sliced avocado", "Hot sauce"],
                "steps": [
                    "Reheat rice, chicken, and beans separately.",
                    "Build the bowl and drizzle green sauce generously over everything.",
                    "Add avocado and a squeeze of lime.",
                ],
                "tip": "The green sauce is what makes this ] make a double batch and use it on everything all week.",
            },
            {
                "name": "Grain Bowl Burrito",
                "subtitle": "wrap everything in a large flour tortilla with cheese and guac",
                "extras": ["Large flour tortillas", "Shredded cheese", "Guacamole or sliced avocado", "Sour cream"],
                "steps": [
                    "Warm tortilla in a skillet.",
                    "Layer rice, chicken, beans, tomatoes, and kale down the center.",
                    "Add cheese, guac, and a drizzle of green sauce. Fold and roll tightly.",
                ],
                "tip": "Toast the rolled burrito seam-side down in the skillet 1 minute [ it seals it and gives a crispy exterior.",
            },
            {
                "name": "Grain Bowl Salad",
                "subtitle": "skip the rice and serve over a big kale salad with the green sauce as dressing",
                "extras": ["4 cups kale, massaged", "Cotija or feta crumbles", "Tortilla chips", "Lime"],
                "steps": [
                    "Massage kale with a drizzle of olive oil and lime juice until slightly softened.",
                    "Top with cold chicken, black beans, tomatoes, red onion, and cheese.",
                    "Drizzle with green sauce and add tortilla chips for crunch.",
                ],
                "tip": "Massage the kale until it darkens slightly ] it transforms from tough and bitter to tender and sweet.",
            },
        ],
    },
    {
        "_id": "mexican-chorizo-casserole",
        "_keywords": ["chorizo", "breakfast", "casserole", "mexican", "eggs", "cheese", "peppers", "meal prep"],
        "image": "/static/images/mexican-chorizo-casserole.jpg",
        "intro": "Spiced Mexican chorizo crumbled with peppers and onions, baked with eggs and melted cheese into a hearty breakfast casserole that slices cleanly and reheats perfectly all week.",
        "base": {
            "title": "Mexican Chorizo Breakfast Casserole",
            "ingredients": [
                "1 lb fresh Mexican chorizo, casings removed",
                "10 large eggs",
                "1/2 cup milk",
                "1 cup shredded Mexican cheese blend or pepper jack",
                "1 red bell pepper, diced",
                "1 green bell pepper, diced",
                "1 jalapeño, minced (seeds removed for mild)",
                "1/2 yellow onion, diced",
                "3 cloves garlic, minced",
                "1 tsp cumin",
                "1/2 tsp smoked paprika",
                "Salt and pepper",
                "Cooking spray",
                "[ To Serve ]",
                "Salsa or hot sauce",
                "Sour cream",
                "Fresh cilantro",
                "Sliced avocado",
            ],
            "steps": [
                "Preheat oven to 375°F. Spray a 9×13 baking dish.",
                "Cook chorizo in a skillet over medium-high, breaking it up, until browned, 6–8 minutes. Remove and drain excess fat.",
                "In the same skillet, sauté onion, peppers, and jalapeño 4–5 minutes until soft. Add garlic, cumin, and paprika [ stir 1 minute.",
                "Spread chorizo and vegetable mixture evenly in the baking dish.",
                "Whisk eggs with milk, salt, and pepper. Pour over the chorizo mixture.",
                "Top with shredded cheese.",
                "Bake 30–35 minutes until eggs are set and cheese is golden. Let cool 10 minutes before slicing.",
            ],
        },
        "uses": [
            {
                "name": "Classic Casserole Slice",
                "subtitle": "a warm slice with salsa, sour cream, and fresh cilantro",
                "extras": ["Salsa or pico de gallo", "Sour cream", "Fresh cilantro", "Sliced avocado"],
                "steps": [
                    "Reheat a slice in the microwave 90 seconds or in the oven at 350°F for 10 minutes.",
                    "Top with salsa, sour cream, and fresh cilantro.",
                    "Serve with sliced avocado on the side.",
                ],
                "tip": "The casserole actually slices more cleanly when cold from the fridge ] heat individual slices rather than the whole dish.",
            },
            {
                "name": "Breakfast Casserole Burrito",
                "subtitle": "wrap a slice in a warm tortilla with guac and hot sauce",
                "extras": ["Flour tortillas", "Guacamole", "Hot sauce", "Shredded lettuce"],
                "steps": [
                    "Warm tortilla in a dry skillet.",
                    "Crumble or lay a slice of casserole down the center.",
                    "Add guacamole, hot sauce, and shredded lettuce. Roll tightly.",
                ],
                "tip": "Crumbling the casserole into the burrito distributes it better than laying a whole slice flat.",
            },
            {
                "name": "Casserole over Hash Browns",
                "subtitle": "serve a slice over crispy hash browns for a loaded brunch plate",
                "extras": ["Frozen hash browns, cooked crispy", "Sour cream", "Pickled jalapeños", "Hot sauce"],
                "steps": [
                    "Cook hash browns in a skillet with oil until very crispy.",
                    "Lay a warmed slice of casserole over the hash browns.",
                    "Top with sour cream, pickled jalapeños, and hot sauce.",
                ],
                "tip": "The crispy hash browns under the soft casserole are the textural contrast that makes this a full brunch.",
            },
        ],
    },
    {
        "_id": "mexican-street-corn",
        "_keywords": ["street corn", "elote", "corn", "mexican", "cotija", "lime", "chili", "lunch", "snack", "dip"],
        "image": "/static/images/mexican-street-corn.jpg",
        "intro": "Charred corn tossed with cotija, lime, chili powder, and a creamy mayo-sour cream sauce [ Mexican street corn off the cob, packed and ready to eat with chips all week.",
        "base": {
            "title": "Mexican Street Corn Dip",
            "ingredients": [
                "4 cups corn kernels (from 4 cobs or frozen, thawed)",
                "1/4 cup mayonnaise",
                "1/4 cup sour cream or Mexican crema",
                "1/2 cup cotija cheese, crumbled",
                "Juice of 1 lime",
                "1 tsp chili powder",
                "1/2 tsp smoked paprika",
                "1/4 tsp garlic powder",
                "2 scallions, thinly sliced",
                "3 tbsp fresh cilantro, chopped",
                "Chili flakes or Tajín, to taste",
                "Tortilla chips, to serve",
            ],
            "steps": [
                "Heat a large cast iron or skillet over high heat. Add corn in a single layer ] do not stir for 2–3 minutes until charred. Toss and char the other side. Work in batches for best charring.",
                "Let corn cool 5 minutes.",
                "Mix mayo and sour cream together in a large bowl.",
                "Add warm corn, cotija, lime juice, chili powder, paprika, garlic powder, scallions, and cilantro. Toss to combine.",
                "Taste and adjust [ more lime, chili, or cotija as needed.",
                "Divide into meal prep containers with tortilla chips on the side.",
            ],
        },
        "uses": [
            {
                "name": "Dip with Chips",
                "subtitle": "the classic ] street corn dip straight with a big handful of chips",
                "extras": ["Tortilla chips", "Extra cotija", "Extra Tajín or chili powder", "Lime wedge"],
                "steps": [
                    "Serve cold or at room temperature in a bowl.",
                    "Extra cotija crumbled on top and a fresh pinch of chili powder.",
                    "Eat with chips for scooping.",
                ],
                "tip": "Keep chips separate until eating [ they go soggy in a few hours if packed together.",
            },
            {
                "name": "Street Corn Bowl",
                "subtitle": "serve over cilantro rice with black beans and grilled chicken",
                "extras": ["1.5 cups cilantro rice", "1/2 cup black beans", "Grilled chicken, sliced", "Sliced avocado"],
                "steps": [
                    "Build a bowl: cilantro rice, black beans, sliced chicken.",
                    "Add a generous scoop of street corn dip over the top.",
                    "Add avocado and extra lime.",
                ],
                "tip": "The street corn dip acts as the dressing for the whole bowl ] no extra sauce needed.",
            },
            {
                "name": "Street Corn Tacos",
                "subtitle": "use the corn dip as a topping for chicken or shrimp tacos",
                "extras": ["Corn or flour tortillas", "Grilled shrimp or chicken", "Shredded cabbage", "Lime wedges"],
                "steps": [
                    "Warm tortillas in a skillet.",
                    "Add shrimp or chicken to each tortilla.",
                    "Top with a spoonful of street corn dip and shredded cabbage.",
                    "Finish with a squeeze of lime.",
                ],
                "tip": "A tablespoon of street corn dip on a taco replaces both the salsa and the slaw.",
            },
        ],
    },
    {
        "_id": "spanish-chicken",
        "_keywords": ["chicken", "chorizo", "olives", "tomatoes", "white beans", "spanish", "dinner", "one pot"],
        "image": "/static/images/spanish-chicken.webp",
        "intro": "Golden chicken thighs braised with chorizo, green olives, white beans, and cherry tomatoes in a rich tomato sauce [ a one-pan Spanish dinner that's weeknight easy and dinner-party impressive.",
        "base": {
            "title": "Easy Spanish Chicken",
            "ingredients": [
                "2.5 lbs bone-in, skin-on chicken thighs",
                "5 oz Spanish chorizo, sliced into rounds",
                "1 can (15 oz) white beans (cannellini or butter beans), drained",
                "1 pint cherry tomatoes",
                "1/2 cup green olives",
                "1 yellow onion, sliced",
                "1 red bell pepper, sliced",
                "4 cloves garlic, sliced",
                "1 cup dry white wine",
                "1 can (14 oz) crushed tomatoes",
                "1 tsp smoked paprika",
                "1 tsp dried thyme",
                "2 tbsp olive oil",
                "Salt and pepper",
                "Fresh parsley, to serve",
            ],
            "steps": [
                "Season chicken with smoked paprika, salt, and pepper.",
                "Heat olive oil in a large wide pan or Dutch oven over medium-high. Sear chicken skin-side down 6–8 minutes until deep golden. Flip and cook 3 more minutes. Remove.",
                "In the same pan, cook chorizo 2–3 minutes until edges crisp. Remove.",
                "Cook onion and pepper in the chorizo fat 4 minutes until soft. Add garlic and stir 1 minute.",
                "Pour in white wine and scrape up any browned bits. Simmer 2 minutes.",
                "Add crushed tomatoes, cherry tomatoes, thyme, olives, and white beans. Stir.",
                "Return chicken and chorizo to the pan, nestling them into the sauce.",
                "Cover and simmer 25–30 minutes until chicken is cooked through and sauce has thickened.",
                "Finish with fresh parsley and serve.",
            ],
        },
        "uses": [
            {
                "name": "Straight from the Pan",
                "subtitle": "served directly with crusty bread to mop up the sauce",
                "extras": ["Crusty bread or baguette", "Extra fresh parsley", "Lemon wedge", "Glass of white wine"],
                "steps": [
                    "Bring the pan to the table.",
                    "Serve each portion with a piece of chicken, chorizo, olives, and plenty of sauce.",
                    "Use crusty bread to soak up the tomato-chorizo pan juices.",
                ],
                "tip": "The sauce is the star ] make sure everyone gets plenty of it.",
            },
            {
                "name": "Over Rice",
                "subtitle": "serve the chicken and sauce over fluffy white rice for a heartier meal",
                "extras": ["2 cups white rice, cooked", "Extra parsley", "Lemon wedge"],
                "steps": [
                    "Cook rice and divide into bowls.",
                    "Ladle chicken, chorizo, beans, and plenty of sauce over the rice.",
                    "Finish with parsley and lemon.",
                ],
                "tip": "Use the sauce as a built-in gravy over the rice [ this version is more filling and works great for meal prep.",
            },
            {
                "name": "Shredded Chicken Flatbread",
                "subtitle": "pull the chicken off the bone and pile onto toasted flatbread with the sauce",
                "extras": ["Flatbreads or pita", "Extra sauce warmed down thick", "Arugula", "Shaved Manchego"],
                "steps": [
                    "Pull chicken from the bones and shred roughly.",
                    "Reduce the sauce in the pan until very thick.",
                    "Toast flatbreads, spoon sauce over, pile with shredded chicken, arugula, and Manchego.",
                ],
                "tip": "Reducing the sauce makes it jammy enough to spread ] this turns the braise into something completely different.",
            },
        ],
    },
    {
        "_id": "gambas-al-ajillo",
        "_keywords": ["shrimp", "garlic", "spanish", "gambas", "olive oil", "chili", "tapas", "dinner"],
        "image": "/static/images/gambas-al-ajillo.webp",
        "intro": "Plump shrimp sizzled in a cazuela of hot olive oil with garlic, dried chili, and a splash of sherry [ the definitive Spanish tapas dish, on the table in 10 minutes.",
        "base": {
            "title": "Gambas al Ajillo (Spanish Garlic Shrimp)",
            "ingredients": [
                "1.5 lbs large shrimp, peeled and deveined (tails on)",
                "8 cloves garlic, thinly sliced",
                "2–3 dried red chilies or 1/2 tsp chili flakes",
                "1/2 cup good-quality olive oil",
                "3 tbsp dry sherry (fino or manzanilla) or dry white wine",
                "1 tsp smoked paprika",
                "Salt",
                "Fresh flat-leaf parsley, roughly chopped",
                "Crusty bread, to serve",
            ],
            "steps": [
                "Pat shrimp completely dry and season with salt.",
                "Heat olive oil in a wide skillet or cazuela over medium. Add garlic and chilies.",
                "Cook slowly, stirring, 3–4 minutes until garlic is light golden and very fragrant. Do not let it burn.",
                "Raise heat to high. Add shrimp in a single layer.",
                "Cook 1 minute, flip, add sherry and paprika, and cook 1 more minute until shrimp are just pink and curled.",
                "Remove immediately from heat ] they finish cooking in the hot oil.",
                "Scatter with fresh parsley. Serve sizzling in the pan with crusty bread.",
            ],
        },
        "uses": [
            {
                "name": "Classic Tapas",
                "subtitle": "sizzling in the pan with crusty bread to soak up the garlic oil",
                "extras": ["Crusty bread or baguette", "Extra parsley", "Lemon wedge"],
                "steps": [
                    "Bring the skillet or cazuela directly to the table.",
                    "Scatter parsley and serve with torn crusty bread for dipping in the garlic-chili oil.",
                ],
                "tip": "The oil is half the dish [ make sure everyone dips their bread into it.",
            },
            {
                "name": "Shrimp and Rice Bowl",
                "subtitle": "serve over saffron rice with a drizzle of the garlic oil",
                "extras": ["2 cups saffron or turmeric rice", "Extra garlic oil from pan", "Lemon wedge", "Parsley"],
                "steps": [
                    "Cook saffron rice: add a pinch of saffron to the cooking water.",
                    "Plate rice in bowls. Lay shrimp over the top.",
                    "Spoon garlic oil from the pan generously over the shrimp and rice.",
                ],
                "tip": "Don't waste a drop of that garlic oil ] it's liquid gold poured over the rice.",
            },
            {
                "name": "Shrimp Pasta",
                "subtitle": "toss the shrimp and garlic oil with linguine or spaghetti",
                "extras": ["300g linguine or spaghetti, cooked al dente", "Splash of pasta water", "Extra parsley", "Lemon zest"],
                "steps": [
                    "Cook pasta and reserve 1/2 cup pasta water.",
                    "Make gambas. Add drained pasta directly to the pan.",
                    "Toss everything together, adding pasta water a splash at a time to create a silky sauce.",
                    "Finish with lemon zest and extra parsley.",
                ],
                "tip": "Add the pasta while the pan is still hot [ the starchy water emulsifies the garlic oil into a proper sauce.",
            },
        ],
    },
    {
        "_id": "spanish-garlic-soup",
        "_keywords": ["soup", "garlic", "egg", "croutons", "spanish", "sopa de ajo", "paprika", "dinner", "bread"],
        "image": "",
        "intro": "Sopa de Ajo ] the ancient Spanish bread and garlic soup with smoked paprika, poached eggs, and crispy croutons. Humble ingredients, deeply warming, ready in 20 minutes.",
        "base": {
            "title": "Spanish Garlic Soup with Egg and Croutons",
            "ingredients": [
                "6 eggs",
                "8–10 cloves garlic, thinly sliced",
                "4 thick slices stale crusty bread, torn into chunks",
                "6 cups chicken broth",
                "3 tbsp olive oil",
                "2 tsp smoked paprika",
                "1/4 tsp cayenne (optional)",
                "Salt and pepper",
                "Fresh parsley, to finish",
            ],
            "steps": [
                "Heat olive oil in a large pot or deep skillet over medium. Add garlic and cook slowly, stirring, 3–4 minutes until golden and fragrant.",
                "Add smoked paprika and cayenne [ stir 30 seconds to bloom.",
                "Add bread chunks and toss to coat in the paprika oil. Toast 2–3 minutes.",
                "Pour in chicken broth. Bring to a simmer and cook 8–10 minutes until bread begins to dissolve and thicken the broth slightly.",
                "Make wells in the soup and crack one egg into each. Cover and simmer 4–5 minutes until whites are set but yolks are still runny.",
                "Season with salt and pepper. Ladle into bowls, making sure each portion has an egg. Finish with fresh parsley.",
            ],
        },
        "uses": [
            {
                "name": "Classic Sopa de Ajo",
                "subtitle": "a bowl of the soup with a poached egg and torn crouton bread",
                "extras": ["Extra crispy croutons", "Smoked paprika pinch", "Fresh parsley", "Crusty bread on the side"],
                "steps": [
                    "Ladle into deep bowls, making sure each bowl has an egg.",
                    "Break the yolk into the hot broth ] it enriches the soup instantly.",
                    "Finish with a pinch of smoked paprika and parsley.",
                ],
                "tip": "The yolk is the seasoning [ break it gently into the broth and stir as you eat.",
            },
            {
                "name": "Thicker Bread Soup",
                "subtitle": "add extra bread and cook longer for a very thick, almost porridge-like version",
                "extras": ["2 extra slices stale bread, crumbled", "Extra smoked paprika", "Manchego shavings"],
                "steps": [
                    "Add extra bread and simmer 5 more minutes until the soup is thick.",
                    "Stir vigorously ] the bread will fully dissolve into the broth.",
                    "Serve topped with Manchego shavings and extra paprika.",
                ],
                "tip": "This thicker version is what's traditionally made in the Spanish countryside [ it's sustaining and filling on a cold night.",
            },
            {
                "name": "Soup with Chorizo",
                "subtitle": "add sliced chorizo to the broth for a more substantial meal",
                "extras": ["3 oz Spanish chorizo, sliced", "Extra broth if needed", "Crusty bread"],
                "steps": [
                    "After toasting the bread, add chorizo and cook 2 minutes until it renders some fat.",
                    "Add broth and continue as the base recipe.",
                    "The chorizo fat will color the broth red and add a smoky depth.",
                ],
                "tip": "Adding chorizo turns a humble peasant soup into a proper dinner ] the fat it renders does the heavy lifting.",
            },
        ],
    },
    {
        "_id": "spanish-beef-rice",
        "_keywords": ["beef", "rice", "spanish", "ground beef", "peppers", "tomatoes", "meal prep", "lunch"],
        "image": "/static/images/spanish-beef-rice.jpg",
        "intro": "Seasoned ground beef cooked with green peppers, onion, and tomatoes into a savory Spanish-style rice [ a classic high-protein meal prep that portions easily and reheats perfectly.",
        "base": {
            "title": "Spanish Beef Rice",
            "ingredients": [
                "1.5 lbs lean ground beef (90/10)",
                "1.5 cups long-grain white rice",
                "2 green bell peppers, diced",
                "1 yellow onion, diced",
                "4 cloves garlic, minced",
                "1 can (14 oz) diced tomatoes",
                "2 cups beef broth",
                "1 tsp smoked paprika",
                "1 tsp cumin",
                "1/2 tsp oregano",
                "1/2 tsp garlic powder",
                "Salt and pepper",
                "2 tbsp olive oil",
                "Fresh parsley, to garnish",
            ],
            "steps": [
                "Heat olive oil in a large deep skillet over medium-high. Brown ground beef, breaking it up, until cooked through. Drain excess fat.",
                "Add onion and green peppers. Cook 4–5 minutes until softened.",
                "Add garlic, smoked paprika, cumin, oregano, and garlic powder. Stir 1 minute.",
                "Add diced tomatoes and stir, scraping up any browned bits.",
                "Add uncooked rice and beef broth. Stir to combine.",
                "Bring to a boil, then reduce heat to low. Cover and cook 18–20 minutes until rice has absorbed the liquid and is fully cooked.",
                "Fluff with a fork. Season with salt and pepper. Garnish with fresh parsley.",
                "Divide into meal prep containers ] makes 4–5 portions.",
            ],
        },
        "uses": [
            {
                "name": "Classic Meal Prep Bowl",
                "subtitle": "straight portions of the beef rice, reheated with extra salsa or hot sauce",
                "extras": ["Salsa or hot sauce", "Fresh parsley", "Sliced avocado (optional)"],
                "steps": [
                    "Reheat in the microwave 2–3 minutes, stirring halfway.",
                    "Top with a spoonful of salsa and fresh parsley.",
                    "Add avocado if you have it.",
                ],
                "tip": "This recipe scales well [ double it for 8–10 meals with no extra effort.",
            },
            {
                "name": "Stuffed Bell Peppers",
                "subtitle": "use the beef rice as the filling for baked stuffed peppers",
                "extras": ["4 bell peppers, tops cut off and seeds removed", "1/2 cup shredded cheese", "Salsa"],
                "steps": [
                    "Preheat oven to 375°F.",
                    "Stuff each pepper with the beef rice and top with shredded cheese.",
                    "Bake 25–30 minutes until peppers are soft and cheese is bubbly.",
                    "Serve with salsa.",
                ],
                "tip": "Use slightly undercooked rice when stuffing ] it finishes cooking inside the pepper without going mushy.",
            },
            {
                "name": "Beef Rice Skillet with Egg",
                "subtitle": "reheat in a skillet and crack eggs on top for a one-pan breakfast-for-dinner",
                "extras": ["2–3 eggs per serving", "Hot sauce", "Fresh cilantro", "Sliced scallions"],
                "steps": [
                    "Add beef rice to a hot skillet with a splash of water. Stir until heated through.",
                    "Make wells and crack in eggs. Cover and cook 3–4 minutes until whites are set.",
                    "Finish with hot sauce, cilantro, and scallions.",
                ],
                "tip": "A cast iron skillet gives the rice a slightly crispy bottom layer [ a bonus texture.",
            },
        ],
    },
    {
        "_id": "peanut-chicken-noodle-bowls",
        "_keywords": ["chicken", "noodle", "peanut", "vietnamese", "cabbage", "carrots"],
        "image": "/static/images/peanut-chicken-noodle-bowls.jpg",
        "intro": "Chicken tossed with noodles, shredded carrots, red cabbage, and a rich peanut sauce. Keep extra sauce on the side so the noodles stay loose all week.",
        "base": {
            "title": "Peanut Chicken Noodle Bowls",
            "ingredients": [
                "1.5 lbs boneless chicken thighs or breasts",
                "8 oz rice noodles or spaghetti",
                "2 cups shredded red cabbage",
                "2 cups shredded carrots",
                "4 scallions, sliced",
                "1/2 cup roasted peanuts, roughly chopped",
                "Fresh cilantro, for serving",
                "1 lime, cut into wedges",
                "1/2 cup peanut butter (creamy)",
                "3 tbsp soy sauce",
                "2 tbsp lime juice",
                "1 tbsp sesame oil",
                "1 tbsp honey or brown sugar",
                "2 cloves garlic, minced",
                "1 tsp fresh ginger, grated",
                "1/4-1/2 cup warm water (to thin)",
                "Chili flakes, to taste",
            ],
            "steps": [
                "Cook chicken: season with salt and pepper, then grill, pan-sear, or bake at 400 F until cooked through. Rest 5 minutes and slice or shred.",
                "Cook noodles according to package directions. Drain and rinse with cold water to stop cooking.",
                "Make peanut sauce: whisk peanut butter, soy sauce, lime juice, sesame oil, honey, garlic, and ginger. Add warm water one tablespoon at a time until pourable. Season with chili flakes.",
                "Toss cooled noodles with half the peanut sauce.",
                "Portion noodles into containers. Top with chicken, cabbage, carrots, and scallions.",
                "Pack remaining sauce separately. Garnish with peanuts, cilantro, and lime.",
            ],
        },
        "uses": [
            {
                "name": "Peanut Noodle Bowl",
                "subtitle": "classic meal prep bowl served cold or at room temperature",
                "extras": ["Extra peanut sauce", "Sliced jalapeño", "Sesame seeds"],
                "steps": [
                    "Remove from fridge 10 minutes before eating.",
                    "Drizzle with extra peanut sauce and toss.",
                    "Top with scallions, peanuts, and a squeeze of lime.",
                ],
                "tip": "Cold noodles firm up in the fridge - let them sit a few minutes and toss with extra sauce before eating.",
            },
            {
                "name": "Lettuce Wraps",
                "subtitle": "spoon the noodle filling into butter lettuce cups for a lighter lunch",
                "extras": ["Butter lettuce leaves", "Hoisin sauce", "Sliced cucumber"],
                "steps": [
                    "Fill lettuce cups with noodles and chicken.",
                    "Add cucumber slices and a drizzle of hoisin.",
                    "Fold and eat immediately.",
                ],
            },
            {
                "name": "Peanut Noodle Stir-Fry",
                "subtitle": "reheat in a hot wok for a warm version with wok-blistered edges",
                "extras": ["1 tsp sesame oil", "Extra soy sauce", "Fried egg (optional)"],
                "steps": [
                    "Heat sesame oil in a wok over high until smoking.",
                    "Add noodles and toss for 2 minutes until edges crisp slightly.",
                    "Serve with a fried egg on top.",
                ],
            },
        ],
    },
    {
        "_id": "crispy-pork-banh-mi",
        "_keywords": ["pork", "banh mi", "vietnamese", "sandwich", "pork belly", "baguette"],
        "image": "/static/images/crispy-pork-banh-mi.jpg",
        "intro": "Pork belly slow-roasted until the skin blisters into crackling, then piled into a crusty baguette with pickled daikon, carrots, cilantro, cucumber, and sriracha mayo.",
        "base": {
            "title": "Crispy Pork Belly Banh Mi",
            "ingredients": [
                "2 lbs pork belly, skin on",
                "1 tbsp five spice powder",
                "1 tbsp fish sauce",
                "4 cloves garlic, minced",
                "1 tsp salt",
                "2 tsp baking soda (for skin)",
                "1 cup daikon radish, julienned",
                "1 cup carrots, julienned",
                "1/4 cup rice vinegar",
                "2 tbsp sugar",
                "1 tsp salt (for pickling)",
                "4 Vietnamese baguettes or sandwich rolls",
                "1/2 cup mayonnaise + 1 tbsp sriracha",
                "1 cucumber, sliced lengthwise",
                "Fresh cilantro",
                "1-2 jalapeños, sliced",
            ],
            "steps": [
                "Score pork skin deeply. Rub underside with five spice, fish sauce, and garlic. Pat skin dry and rub with baking soda and salt. Refrigerate uncovered overnight or at least 4 hours.",
                "Preheat oven to 300 F. Place pork skin-side up on a rack. Roast 2 hours until tender.",
                "Increase oven to 450 F (or use broiler). Roast another 20-25 minutes until skin is deeply blistered and crispy. Rest 15 minutes before slicing.",
                "Pickle the daikon and carrots: mix with rice vinegar, sugar, and salt. Refrigerate at least 30 minutes.",
                "Mix sriracha mayo. Slice baguettes and spread generously with mayo.",
                "Layer cucumber, pickled daikon-carrot, sliced pork belly, cilantro, and jalapeño.",
            ],
        },
        "uses": [
            {
                "name": "Banh Mi Sandwich",
                "subtitle": "the classic - crispy pork in a crusty baguette with all the toppings",
                "extras": ["Sriracha mayo", "Extra pickled vegetables", "Fresh cilantro"],
                "steps": [
                    "Reheat pork belly slices in a 400 F oven for 8 minutes or in a dry pan until skin crisps again.",
                    "Build the banh mi just before eating so the bread stays crusty.",
                ],
                "tip": "Store pork belly separately from bread. Reheat in a pan or oven - microwaving destroys the crackling.",
            },
            {
                "name": "Pork Belly Rice Bowl",
                "subtitle": "serve the crispy pork over jasmine rice with nuoc cham and pickled veg",
                "extras": ["Jasmine rice", "Nuoc cham (fish sauce, lime, sugar, garlic, chili, water)", "Sliced cucumber"],
                "steps": [
                    "Reheat pork belly until skin is crispy again.",
                    "Serve over rice with pickled daikon and carrots.",
                    "Drizzle with nuoc cham.",
                ],
            },
            {
                "name": "Pork Belly Lettuce Wraps",
                "subtitle": "lighter, gluten-free version with butter lettuce and hoisin",
                "extras": ["Butter lettuce leaves", "Hoisin sauce", "Sliced scallions", "Crushed peanuts"],
                "steps": [
                    "Slice pork belly thin and reheat in a pan.",
                    "Serve in lettuce cups with pickled veg, hoisin, and peanuts.",
                ],
            },
        ],
    },
    {
        "_id": "lemongrass-chicken-rolls",
        "_keywords": ["chicken", "lemongrass", "rice paper", "rolls", "vietnamese", "spring rolls"],
        "image": "/static/images/lemongrass-chicken-rolls.jpg",
        "intro": "Lemongrass-marinated chicken strips, rice vermicelli, and a bouquet of fresh herbs wrapped in translucent rice paper. One of the freshest lunches you can meal prep.",
        "base": {
            "title": "Lemongrass Chicken Rice Paper Rolls",
            "ingredients": [
                "1.5 lbs boneless chicken thighs",
                "2 stalks lemongrass (white part only), finely minced",
                "3 cloves garlic, minced",
                "2 tbsp fish sauce",
                "1 tbsp sugar",
                "1 tbsp lime juice",
                "1 tbsp neutral oil",
                "4 oz rice vermicelli",
                "16 rice paper wrappers (22cm)",
                "2 cups shredded red cabbage",
                "2 carrots, julienned",
                "1 cucumber, julienned",
                "Fresh mint, basil, and cilantro",
                "1/2 cup peanut butter",
                "2 tbsp hoisin sauce",
                "1 tbsp lime juice + warm water to thin",
            ],
            "steps": [
                "Marinate chicken: combine lemongrass, garlic, fish sauce, sugar, lime juice, and oil. Add chicken and marinate at least 30 minutes or overnight.",
                "Grill or pan-sear chicken over high heat 4-5 minutes per side until cooked through and charred at edges. Rest and slice thin.",
                "Cook vermicelli according to package. Drain and rinse cold. Toss with a little sesame oil to prevent sticking.",
                "Make peanut-hoisin sauce: whisk peanut butter, hoisin, and lime juice. Thin with warm water until pourable.",
                "To roll: briefly dip rice paper in warm water (5-6 seconds). Lay flat. Layer herbs, noodles, chicken, and vegetables across the lower third. Fold sides in and roll up tightly.",
                "Store rolls in a single layer, wrapped in damp paper towel and covered.",
            ],
        },
        "uses": [
            {
                "name": "Fresh Rice Paper Rolls",
                "subtitle": "serve with peanut-hoisin dipping sauce",
                "extras": ["Peanut-hoisin dipping sauce", "Sliced chili", "Crushed peanuts"],
                "steps": [
                    "Remove from fridge 10 minutes before eating.",
                    "Dip generously in peanut-hoisin sauce.",
                ],
                "tip": "Rolls dry out in the fridge - wrap each one individually in plastic wrap or a damp paper towel.",
            },
            {
                "name": "Lemongrass Chicken Noodle Salad",
                "subtitle": "skip the rolling and toss everything together as a salad",
                "extras": ["Extra nuoc cham dressing", "Crushed peanuts", "Fried shallots"],
                "steps": [
                    "Combine vermicelli, vegetables, herbs, and sliced chicken in a large bowl.",
                    "Dress with nuoc cham (fish sauce, lime, sugar, garlic, chili, water).",
                    "Top with crushed peanuts and fried shallots.",
                ],
            },
            {
                "name": "Lemongrass Chicken Lettuce Cups",
                "subtitle": "use butter lettuce instead of rice paper for a quick no-roll version",
                "extras": ["Butter lettuce leaves", "Hoisin sauce", "Lime wedges"],
                "steps": [
                    "Fill butter lettuce cups with noodles, chicken, and vegetables.",
                    "Drizzle with peanut-hoisin sauce and squeeze lime.",
                ],
            },
        ],
    },
    {
        "_id": "vietnamese-pork-noodle-bowls",
        "_keywords": ["pork", "noodle", "vietnamese", "bun", "lemongrass", "caramelized"],
        "image": "/static/images/vietnamese-pork-noodle-bowls.jpg",
        "intro": "Caramelized pork marinated in lemongrass and fish sauce, served over rice vermicelli with crispy shallots, bean sprouts, cucumber, and tangy nuoc cham.",
        "base": {
            "title": "Vietnamese Pork Noodle Bowls",
            "ingredients": [
                "1.5 lbs pork shoulder or pork tenderloin",
                "2 stalks lemongrass, minced",
                "3 cloves garlic, minced",
                "2 shallots, minced",
                "3 tbsp fish sauce",
                "1 tbsp soy sauce",
                "1.5 tbsp sugar",
                "1 tbsp neutral oil",
                "8 oz rice vermicelli",
                "2 cups bean sprouts",
                "1 cucumber, julienned",
                "Fresh mint, cilantro, and perilla",
                "Crispy fried shallots",
                "Crushed roasted peanuts",
                "Sliced fresh chili",
                "3 tbsp fish sauce, 2 tbsp lime juice, 2 tbsp sugar, 1 clove garlic, 1 chili, 3 tbsp water (nuoc cham)",
            ],
            "steps": [
                "Slice pork thin against the grain (about 1/4 inch). Combine with lemongrass, garlic, shallots, fish sauce, soy sauce, sugar, and oil. Marinate at least 1 hour, ideally overnight.",
                "Make nuoc cham: combine fish sauce, lime juice, sugar, minced garlic, sliced chili, and water. Stir until sugar dissolves. Taste and adjust.",
                "Cook vermicelli according to package. Drain and rinse cold.",
                "Grill or sear pork in batches over high heat until caramelized with charred edges, about 2-3 minutes per side.",
                "Portion noodles into bowls. Top with pork, bean sprouts, cucumber, and herbs.",
                "Drizzle with nuoc cham. Garnish with crispy shallots, peanuts, and chili.",
            ],
        },
        "uses": [
            {
                "name": "Bun Thit Nuong Bowl",
                "subtitle": "the classic bowl - noodles, pork, herbs, peanuts, nuoc cham",
                "extras": ["Extra nuoc cham", "Crispy fried shallots", "Sliced chili"],
                "steps": [
                    "Reheat pork briefly in a dry pan over medium-high to warm and re-caramelize edges.",
                    "Arrange over cold noodles with fresh toppings.",
                    "Drizzle nuoc cham generously.",
                ],
                "tip": "Nuoc cham is the key - make a big jar and keep it in the fridge. It keeps 1-2 weeks.",
            },
            {
                "name": "Pork Rice Bowl",
                "subtitle": "serve the caramelized pork over jasmine rice instead of noodles",
                "extras": ["Jasmine rice", "Pickled cucumber", "Fried egg"],
                "steps": [
                    "Reheat pork until caramelized edges return.",
                    "Serve over rice with pickled cucumber and a fried egg.",
                    "Drizzle with nuoc cham.",
                ],
            },
            {
                "name": "Pork and Noodle Spring Rolls",
                "subtitle": "wrap leftover pork and noodles in rice paper for an easy lunch",
                "extras": ["Rice paper wrappers", "Butter lettuce", "Peanut-hoisin sauce"],
                "steps": [
                    "Soak rice paper briefly in warm water.",
                    "Fill with leftover pork, noodles, herbs, and cucumber.",
                    "Roll and serve with peanut-hoisin sauce.",
                ],
            },
        ],
    },
    {
        "_id": "vietnamese-chicken-salad",
        "_keywords": ["chicken", "salad", "vietnamese", "cabbage", "herbs", "peanuts", "goi ga"],
        "image": "/static/images/vietnamese-chicken-salad.jpg",
        "intro": "Shredded poached chicken with napa cabbage, fresh herbs, crushed peanuts, crispy fried shallots, and a punchy lime-fish sauce dressing. One of the best salads you can meal prep.",
        "base": {
            "title": "Vietnamese Chicken Salad",
            "ingredients": [
                "1.5 lbs chicken breasts or thighs",
                "3 cups napa cabbage, finely shredded",
                "2 cups red cabbage, finely shredded",
                "2 large carrots, julienned",
                "1/2 red onion, very thinly sliced",
                "1 cup fresh mint leaves",
                "1 cup fresh cilantro leaves",
                "1 cup fresh perilla or Thai basil (optional)",
                "1-2 red chilies, sliced",
                "1/2 cup roasted peanuts, roughly crushed",
                "1/4 cup crispy fried shallots",
                "3 tbsp fish sauce",
                "3 tbsp lime juice",
                "2 tbsp sugar",
                "2 cloves garlic, minced",
                "1-2 bird's eye chilies, minced",
            ],
            "steps": [
                "Poach chicken: place in cold water with a pinch of salt. Bring to a simmer, cook 15 minutes. Remove and let cool in the broth. Shred by hand into thin strips.",
                "Soak red onion slices in cold salted water for 10 minutes to mellow. Drain and pat dry.",
                "Make dressing: combine fish sauce, lime juice, sugar, garlic, and chili. Stir until sugar dissolves. Taste - it should be tangy, salty, and slightly sweet.",
                "Combine cabbage, carrots, and onion in a large bowl. Toss with half the dressing.",
                "Add shredded chicken and toss again.",
                "Top with mint, cilantro, peanuts, and crispy shallots. Drizzle remaining dressing.",
            ],
        },
        "uses": [
            {
                "name": "Vietnamese Chicken Salad",
                "subtitle": "the classic bowl - eat it straight with chopsticks",
                "extras": ["Extra dressing", "Extra fried shallots", "Lime wedge"],
                "steps": [
                    "If meal prepping, store dressing and crunchy toppings separately.",
                    "Toss dressing in just before eating. Add peanuts and shallots last.",
                ],
                "tip": "This salad is best the day it's dressed. Toss undressed components and add dressing at the last minute if packing for lunch.",
            },
            {
                "name": "Chicken Salad Rice Paper Rolls",
                "subtitle": "wrap the salad tightly in rice paper for a portable version",
                "extras": ["Rice paper wrappers", "Rice vermicelli", "Peanut dipping sauce"],
                "steps": [
                    "Soak rice paper briefly in warm water.",
                    "Fill with a small handful of salad and a few noodles.",
                    "Roll tightly and serve with peanut-hoisin dipping sauce.",
                ],
            },
            {
                "name": "Chicken Salad Noodle Bowl",
                "subtitle": "toss the salad over cold rice vermicelli for a more substantial bowl",
                "extras": ["4 oz rice vermicelli, cooked and cooled", "Nuoc cham", "Extra peanuts"],
                "steps": [
                    "Arrange cold noodles in a bowl.",
                    "Pile chicken salad on top.",
                    "Drizzle nuoc cham and top with extra peanuts and shallots.",
                ],
            },
        ],
    },
    {
        "_id": "pho-saigon",
        "_keywords": ["pho", "beef", "noodle soup", "vietnamese", "broth", "brisket"],
        "image": "/static/images/pho-saigon.jpg",
        "intro": "A Southern Vietnamese pho with a rich, clear beef broth built on charred ginger, star anise, and cinnamon. Ladle over rice noodles and raw beef, then finish with a mountain of fresh herbs.",
        "base": {
            "title": "Pho Saigon",
            "ingredients": [
                "2 lbs beef bones (knuckle and marrow)",
                "1 lb beef brisket",
                "1 large yellow onion, halved",
                "3-inch piece fresh ginger, halved lengthwise",
                "3 star anise",
                "1 cinnamon stick",
                "4 whole cloves",
                "1 tsp coriander seeds",
                "3 tbsp fish sauce",
                "1 tbsp sugar",
                "Salt to taste",
                "8 oz rice noodles (banh pho)",
                "1/2 lb beef eye of round, sliced paper-thin (for raw beef)",
                "Bean sprouts, Thai basil, cilantro, lime, hoisin, sriracha for serving",
            ],
            "steps": [
                "Parboil bones: cover bones in cold water and bring to a boil. Boil 10 minutes. Drain and rinse bones and pot thoroughly.",
                "Char aromatics: place onion and ginger cut-side-down directly over a gas flame or under a broiler until blackened and fragrant, about 5-7 minutes.",
                "Toast spices: dry-toast star anise, cinnamon, cloves, and coriander seeds in a dry pan until fragrant, 1-2 minutes.",
                "Combine bones, brisket, charred aromatics, and toasted spices in a large pot. Cover with 4 quarts of water. Bring to a boil then reduce to a gentle simmer.",
                "Simmer uncovered 3-4 hours, skimming foam and fat regularly. Remove brisket after 2 hours when tender. Strain broth through a fine mesh sieve.",
                "Season broth with fish sauce and sugar. Taste and adjust - it should be deeply savory and aromatic.",
                "Cook noodles according to package. Divide into bowls. Add thinly sliced raw beef. Pour very hot broth over (the heat cooks the beef).",
                "Add sliced brisket. Serve immediately with all garnishes on the side.",
            ],
        },
        "uses": [
            {
                "name": "Classic Pho Bowl",
                "subtitle": "rice noodles, beef, herbs, hoisin, sriracha, lime",
                "extras": ["Bean sprouts", "Thai basil", "Lime wedges", "Hoisin", "Sriracha"],
                "steps": [
                    "Bring broth to a rolling boil before serving.",
                    "Cook noodles fresh for each serving.",
                    "Pour boiling broth over raw beef to cook it in the bowl.",
                ],
                "tip": "The broth stores 5 days in the fridge or 3 months in the freezer. Make a big batch and freeze in portions.",
            },
            {
                "name": "Pho Brisket Rice Bowl",
                "subtitle": "use leftover brisket over jasmine rice with broth as a dipping sauce",
                "extras": ["Jasmine rice", "Pickled jalapeños", "Hoisin sauce"],
                "steps": [
                    "Reheat sliced brisket in broth.",
                    "Serve over rice with a small bowl of broth on the side for dipping.",
                    "Add hoisin and sliced scallions.",
                ],
            },
            {
                "name": "Congee with Pho Broth",
                "subtitle": "use the broth as the base for a silky rice porridge",
                "extras": ["1/2 cup jasmine rice", "Soft-boiled egg", "Ginger, sliced scallions"],
                "steps": [
                    "Simmer 1/2 cup rice in 4 cups pho broth for 45-60 minutes until broken down and creamy.",
                    "Top with soft-boiled egg, sliced ginger, and scallions.",
                ],
            },
        ],
    },
    {
        "_id": "tom-rim-shrimp",
        "_keywords": ["shrimp", "vietnamese", "caramelized", "tom rim", "fish sauce"],
        "image": "/static/images/tom-rim-shrimp.jpg",
        "intro": "Shell-on shrimp caramelized in fish sauce, garlic, shallots, and palm sugar until sticky and deeply glazed. Serve over jasmine rice - this is one of the most satisfying weeknight meals in Vietnamese cooking.",
        "base": {
            "title": "Tom Rim (Vietnamese Caramelized Shrimp)",
            "ingredients": [
                "1.5 lbs large shell-on shrimp (16-20 count)",
                "4 cloves garlic, minced",
                "2 shallots, minced",
                "2 tbsp fish sauce",
                "2 tbsp palm sugar or brown sugar",
                "1 tsp black pepper",
                "2-3 bird's eye chilies, sliced (optional)",
                "2 tbsp neutral oil",
                "3 scallions, sliced",
                "Jasmine rice, for serving",
            ],
            "steps": [
                "Pat shrimp completely dry. This is important for caramelization.",
                "Heat oil in a wok or large skillet over high heat until nearly smoking.",
                "Add garlic and shallots. Stir-fry 30 seconds until fragrant.",
                "Add shrimp in a single layer. Do not touch for 1 minute to allow caramelization.",
                "Add fish sauce, sugar, and black pepper. Toss everything together.",
                "Continue cooking 3-4 minutes, tossing occasionally, until sauce is reduced, sticky, and deeply caramelized.",
                "Add chili and scallions in the last 30 seconds.",
                "Serve immediately over jasmine rice.",
            ],
        },
        "uses": [
            {
                "name": "Tom Rim Rice Bowl",
                "subtitle": "the classic - caramelized shrimp over jasmine rice",
                "extras": ["Jasmine rice", "Sliced cucumber", "Fish sauce on the side"],
                "steps": [
                    "Reheat shrimp gently in a pan with a splash of water to loosen the glaze.",
                    "Serve over fresh rice with cucumber slices.",
                ],
                "tip": "Shell-on shrimp are traditional and keep the shrimp juicier. Peel before eating or serve as-is - the sticky shells are part of the experience.",
            },
            {
                "name": "Shrimp Noodle Bowl",
                "subtitle": "toss the shrimp with rice vermicelli and fresh herbs",
                "extras": ["4 oz rice vermicelli, cooked", "Fresh mint and cilantro", "Nuoc cham"],
                "steps": [
                    "Arrange cold noodles in a bowl.",
                    "Top with reheated shrimp and fresh herbs.",
                    "Drizzle nuoc cham.",
                ],
            },
            {
                "name": "Shrimp Banh Mi",
                "subtitle": "pile the caramelized shrimp into a baguette for a quick sandwich",
                "extras": ["Vietnamese baguette or hoagie roll", "Sriracha mayo", "Pickled daikon and carrots", "Cilantro"],
                "steps": [
                    "Slice roll and spread with sriracha mayo.",
                    "Fill with warm shrimp, pickled vegetables, and cilantro.",
                    "Eat immediately.",
                ],
            },
        ],
    },
    {
        "_id": "canh-chua-ca",
        "_keywords": ["fish", "soup", "vietnamese", "tomato", "dill", "canh chua"],
        "image": "/static/images/canh-chua-ca.jpg",
        "intro": "A Northern Vietnamese fish soup with white fish simmered in a light tomato broth with fresh dill and fish sauce. Delicate and herbaceous - best served over white rice.",
        "base": {
            "title": "Canh Chua Ca (Vietnamese Tomato and Dill Fish Soup)",
            "ingredients": [
                "1.5 lbs white fish fillets (sea bass, catfish, or cod), cut into large pieces",
                "3 medium ripe tomatoes, cut into wedges",
                "3 shallots, minced",
                "3 cloves garlic, minced",
                "2 tbsp fish sauce",
                "1 tbsp neutral oil",
                "3 cups water or light chicken broth",
                "1 large bunch fresh dill, roughly chopped",
                "3 scallions, sliced",
                "Salt and white pepper",
                "Jasmine rice, for serving",
            ],
            "steps": [
                "Season fish with a pinch of salt and white pepper.",
                "Heat oil in a wide saucepan over medium. Saute shallots and garlic until translucent, about 2 minutes.",
                "Add tomatoes and cook until they begin to soften, 3-4 minutes.",
                "Add water or broth and fish sauce. Bring to a gentle simmer.",
                "Add fish pieces. Poach at a gentle simmer for 6-8 minutes - the fish should be just cooked through. Do not boil.",
                "Taste broth and adjust fish sauce for saltiness.",
                "Remove from heat. Stir in dill and scallions.",
                "Serve immediately over jasmine rice.",
            ],
        },
        "uses": [
            {
                "name": "Fish Soup Over Rice",
                "subtitle": "ladle directly over rice - the broth soaks into the grains",
                "extras": ["Jasmine rice", "Extra fresh dill", "Lime wedge", "Fish sauce on the side"],
                "steps": [
                    "Reheat broth gently. Add fish only to warm through - do not reboil.",
                    "Serve over rice with extra fresh dill.",
                ],
                "tip": "This soup is best fresh. If meal prepping, keep the fish and broth separate and combine just before serving.",
            },
            {
                "name": "Light Noodle Soup",
                "subtitle": "add rice vermicelli to turn the soup into a fuller noodle bowl",
                "extras": ["4 oz rice vermicelli, cooked", "Bean sprouts", "Sliced chili"],
                "steps": [
                    "Cook vermicelli separately.",
                    "Reheat broth with fish gently.",
                    "Place noodles in a bowl and ladle soup over.",
                    "Add bean sprouts and chili.",
                ],
            },
        ],
    },
    {
        "_id": "grilled-pork-rice-paper-rolls",
        "_keywords": ["pork", "rice paper", "vietnamese", "nem nuong", "grilled", "pork paste", "skewers"],
        "image": "/static/images/grilled-pork-rice-paper-rolls.jpg",
        "intro": "Vietnamese grilled pork paste skewers wrapped in soft rice paper with pickled daikon, cucumber, herbs, and a rich hoisin-peanut sauce. The pork paste stays tender and juicy off the grill.",
        "base": {
            "title": "Grilled Pork Paste Rice Paper Rolls",
            "ingredients": [
                "1.5 lbs ground pork (not too lean)",
                "2 cloves garlic, minced",
                "2 shallots, minced",
                "1 tbsp fish sauce",
                "1.5 tbsp sugar",
                "1/2 tsp baking powder",
                "1 tsp black pepper",
                "Bamboo skewers, soaked in water 30 min",
                "16 rice paper wrappers (22cm)",
                "4 oz rice vermicelli, cooked",
                "1 cup daikon radish, pickled",
                "1 cucumber, julienned",
                "Fresh mint, Thai basil, cilantro, perilla",
                "1/4 cup hoisin sauce",
                "3 tbsp peanut butter",
                "1-2 tbsp warm water",
                "1 tsp rice vinegar",
                "Crushed peanuts for garnish",
            ],
            "steps": [
                "Combine ground pork, garlic, shallots, fish sauce, sugar, baking powder, and pepper. Mix thoroughly until the paste becomes slightly sticky.",
                "Refrigerate paste at least 30 minutes (or overnight) for best texture.",
                "With wet hands, mold 2-3 tablespoons of paste firmly around each skewer into a thin sausage shape.",
                "Grill over medium-high heat, turning every 2-3 minutes, until cooked through with charred spots, about 10-12 minutes.",
                "Make dipping sauce: whisk hoisin, peanut butter, vinegar, and water until smooth.",
                "To roll: dip rice paper briefly in warm water, lay flat. Layer herbs, a few noodles, cucumber, pickled daikon, and a pork skewer (slid off the stick). Roll up, folding in the sides.",
            ],
        },
        "uses": [
            {
                "name": "Nem Nuong Rice Paper Rolls",
                "subtitle": "the classic - wrapped with fresh herbs and dipped in hoisin-peanut sauce",
                "extras": ["Hoisin-peanut sauce", "Extra herbs", "Sliced chili"],
                "steps": [
                    "Grill pork skewers fresh or reheat in a pan until charred edges return.",
                    "Roll in rice paper with herbs, noodles, and pickled veg.",
                    "Dip generously in sauce.",
                ],
                "tip": "The pork paste mixture keeps raw in the fridge for 2 days. Grill fresh for the best char.",
            },
            {
                "name": "Pork Skewer Rice Bowl",
                "subtitle": "serve the grilled pork over jasmine rice with nuoc cham",
                "extras": ["Jasmine rice", "Nuoc cham", "Cucumber and pickled vegetables"],
                "steps": [
                    "Reheat pork skewers until lightly charred.",
                    "Slide off skewer and serve over rice with pickled daikon, cucumber, and nuoc cham.",
                ],
            },
            {
                "name": "Pork Lettuce Cups",
                "subtitle": "a no-roll option - butter lettuce cups with all the same fillings",
                "extras": ["Butter lettuce leaves", "Hoisin-peanut sauce", "Crushed peanuts"],
                "steps": [
                    "Warm pork skewers, slide off stick, and break into pieces.",
                    "Fill butter lettuce cups with pork, noodles, herbs, and cucumber.",
                    "Drizzle with hoisin-peanut sauce and crushed peanuts.",
                ],
            },
        ],
    },
]


def _find_db_recipe(cluster_summary: dict) -> dict | None:
    """Return a matching RECIPE_DB entry if the cluster's protein/style matches."""
    search_text = " ".join([
        cluster_summary.get("name", ""),
        cluster_summary.get("tagline", ""),
        " ".join(cluster_summary.get("ingredients", [])),
        " ".join(str(m) for m in cluster_summary.get("meals", [])),
    ]).lower()

    for recipe in RECIPE_DB:
        if all(kw.lower() in search_text for kw in recipe["_keywords"]):
            return recipe
    return None


def _current_season() -> str:
    return _SEASON_MAP[date.today().month]


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


# ── Calendar helpers ─────────────────────────────────────────────────────────
DAYS_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
DAYS_FULL  = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
_DAY_LOOKUP = {
    'mon': 0, 'monday': 0,
    'tue': 1, 'tuesday': 1, 'tues': 1,
    'wed': 2, 'wednesday': 2,
    'thu': 3, 'thursday': 3, 'thurs': 3,
    'fri': 4, 'friday': 4,
    'sat': 5, 'saturday': 5,
    'sun': 6, 'sunday': 6,
}


def _week_key(d: date) -> str:
    yr, wk, _ = d.isocalendar()
    return f"{yr}-W{wk:02d}"


def _week_dates(week_key: str) -> list:
    """Returns the 7 dates (Mon–Sun) for the given ISO week key."""
    yr, wk = int(week_key[:4]), int(week_key[6:])
    monday = date.fromisocalendar(yr, wk, 1)
    return [monday + timedelta(days=i) for i in range(7)]


def _month_weeks(year: int, month: int) -> list:
    """Returns list of (week_key, [date x7]) for every week overlapping the month."""
    first = date(year, month, 1)
    last = date(year, month, _cal.monthrange(year, month)[1])
    monday = first - timedelta(days=first.weekday())
    weeks = []
    while monday <= last:
        yr, wk, _ = monday.isocalendar()
        weeks.append((f"{yr}-W{wk:02d}", [monday + timedelta(days=i) for i in range(7)]))
        monday += timedelta(weeks=1)
    return weeks


_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _scale_amount(amount: str, scale: int) -> str:
    """Multiply the leading number in an amount string by scale."""
    if scale <= 1 or not amount:
        return amount
    s = amount.strip()
    m = re.match(r'^(\d+)\s*/\s*(\d+)(.*)', s)
    if m:
        val = int(m.group(1)) / int(m.group(2)) * scale
        rest = m.group(3).strip()
        fmt = str(int(val)) if val == int(val) else f"{val:.2g}"
        return (fmt + (' ' + rest if rest else '')).strip()
    m = re.match(r'^(\d+\.?\d*)(.*)', s)
    if m:
        val = float(m.group(1)) * scale
        rest = m.group(2).strip()
        fmt = str(int(val)) if val == int(val) else f"{val:.2g}"
        return (fmt + (' ' + rest if rest else '')).strip()
    return s


def _build_calendar(accepted: list, meal_prep_days: list = None) -> tuple:
    """
    Returns (calendar, meal_counts).

    Only schedules meals for the selected meal_prep_days (in week order).
    Interleaves meals from cluster A and B (A1, B1, A2, B2, …) and assigns
    one per selected day, wrapping around if there are more days than meals.
    """
    if meal_prep_days is None:
        meal_prep_days = DAYS_SHORT

    selected_indices = [i for i, d in enumerate(DAYS_SHORT) if d in meal_prep_days]
    calendar = [{'short': DAYS_SHORT[i], 'full': DAYS_FULL[i], 'meals': {}} for i in selected_indices]
    meal_counts: dict = {}

    by_type: dict = defaultdict(list)
    for c in accepted:
        mt = c.get('mealType', '')
        if mt:
            by_type[mt].append(c)

    for mt, clusters in by_type.items():
        interleaved = []
        if len(clusters) >= 2:
            c1_meals = clusters[0].get('meals', [])
            c2_meals = clusters[1].get('meals', [])
            for i in range(max(len(c1_meals), len(c2_meals))):
                if i < len(c1_meals) and c1_meals[i].get('name'):
                    interleaved.append((clusters[0]['id'], c1_meals[i]['name']))
                if i < len(c2_meals) and c2_meals[i].get('name'):
                    interleaved.append((clusters[1]['id'], c2_meals[i]['name']))
        else:
            for c in clusters:
                for m in c.get('meals', []):
                    if m.get('name'):
                        interleaved.append((c['id'], m['name']))

        if not interleaved:
            continue

        n = len(interleaved)
        for slot_idx, cal_day in enumerate(calendar):
            cid, meal_name = interleaved[slot_idx % n]
            cal_day['meals'][mt] = meal_name
            key = (cid, meal_name)
            meal_counts[key] = meal_counts.get(key, 0) + 1

    return calendar, meal_counts


# ── General helpers ──────────────────────────────────────────────────────────
def get_anthropic_client():
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    return anthropic.Anthropic(api_key=key)


def get_prefs(request: Request) -> dict:
    return request.session.get("preferences") or {}


def _sets_per_type(prefs: dict) -> int:
    meal_prep_days = prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"])
    return 2 if len(meal_prep_days) >= 5 else 1


def _can_view_plan(clusters: list, meal_types: list, sets_needed: int = 2) -> bool:
    counts: dict = defaultdict(int)
    for c in clusters:
        if c.get("accepted") and not c.get("reserve") and not c.get("skipped"):
            counts[c.get("mealType", "")] += 1
    return bool(meal_types) and all(counts[mt] >= sets_needed for mt in meal_types)


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
    if "plans_by_week" in request.session:
        data["plans_by_week"] = request.session["plans_by_week"]
    for key, val in list(request.session.items()):
        if key.startswith("checked_"):
            data[key] = val
    save_user_data(user_id, data)


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
    if "plans_by_week" in data:
        session_plans = request.session.get("plans_by_week", {})
        request.session["plans_by_week"] = {**data["plans_by_week"], **session_plans}
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

    ctx: dict = {
        "request": request,
        "has_prefs": has_prefs,
        "has_plan": False,
        "carousel_recipes": carousel_recipes,
        "preferences": prefs or {},
        "calendar_data": [],
        "week_range": "",
        "selected_week": "",
        "current_week": "",
        "cal_year": date.today().year,
        "cal_month": date.today().month,
        "cal_month_name": _MONTH_NAMES[date.today().month - 1],
        "month_weeks": _month_weeks(date.today().year, date.today().month),
        "prev_year": date.today().year if date.today().month > 1 else date.today().year - 1,
        "prev_month": date.today().month - 1 if date.today().month > 1 else 12,
        "next_year": date.today().year if date.today().month < 12 else date.today().year + 1,
        "next_month": date.today().month + 1 if date.today().month < 12 else 1,
        "today_str": date.today().isoformat(),
    }

    if has_prefs:
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

        clusters = load_clusters(request)
        accepted = [c for c in clusters if c.get("accepted")]
        meal_prep_days = prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"])
        day_completions = request.session.get("dayCompletions", {})

        calendar_data = []
        if accepted:
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

        mon = week_dates_list[0]
        sun = week_dates_list[6]
        if mon.month == sun.month:
            week_range = f"{mon.strftime('%b')} {mon.day}–{sun.day}, {mon.year}"
        else:
            week_range = f"{mon.strftime('%b')} {mon.day} – {sun.strftime('%b')} {sun.day}, {mon.year}"

        ctx.update({
            "today": today,
            "today_str": today.isoformat(),
            "selected_week": selected_week,
            "current_week": current_week,
            "week_range": week_range,
            "cal_year": cal_year,
            "cal_month": cal_month,
            "cal_month_name": _MONTH_NAMES[cal_month - 1],
            "month_weeks": month_weeks,
            "prev_year": cal_year if cal_month > 1 else cal_year - 1,
            "prev_month": cal_month - 1 if cal_month > 1 else 12,
            "next_year": cal_year if cal_month < 12 else cal_year + 1,
            "next_month": cal_month + 1 if cal_month < 12 else 1,
            "calendar_data": calendar_data,
            "has_plan": bool(accepted),
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


@app.get("/preferences", response_class=HTMLResponse)
async def preferences_page(request: Request):
    prefs = request.session.get("preferences")
    if not prefs:
        return RedirectResponse("/onboarding")
    return templates.TemplateResponse("preferences.html", {
        "request": request,
        "preferences": prefs,
    })


@app.post("/save-preferences")
async def save_preferences(
    request: Request,
    meal_types: List[str] = Form(default=[]),
    servings: int = Form(2),
    dietary_needs: str = Form(""),
    budget_level: str = Form("moderate"),
    meal_prep_days: List[str] = Form(default=[]),
):
    if not meal_types:
        meal_types = ["dinner"]
    if not meal_prep_days:
        meal_prep_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    request.session["preferences"] = {
        "mealTypes": meal_types,
        "servings": servings,
        "dietaryNeeds": dietary_needs.strip(),
        "budgetLevel": budget_level,
        "mealPrepDays": meal_prep_days,
    }
    _persist_user(request)
    return RedirectResponse("/landing", status_code=303)


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "preferences": {}})


@app.post("/setup")
async def setup(
    request: Request,
    meal_types: List[str] = Form(default=[]),
    servings: int = Form(2),
    dietary_needs: str = Form(""),
    budget_level: str = Form("moderate"),
    meal_prep_days: List[str] = Form(default=[]),
):
    if not meal_types:
        meal_types = ["dinner"]
    if not meal_prep_days:
        meal_prep_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    request.session["preferences"] = {
        "mealTypes": meal_types,
        "servings": servings,
        "dietaryNeeds": dietary_needs.strip(),
        "budgetLevel": budget_level,
        "mealPrepDays": meal_prep_days,
    }
    _persist_user(request)
    return RedirectResponse("/mode", status_code=303)


@app.post("/update-settings")
async def update_settings(
    request: Request,
    meal_types: List[str] = Form(default=[]),
    servings: int = Form(2),
    dietary_needs: str = Form(""),
    budget_level: str = Form("moderate"),
    meal_prep_days: List[str] = Form(default=[]),
):
    if not meal_types:
        meal_types = ["dinner"]
    if not meal_prep_days:
        meal_prep_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    request.session["preferences"] = {
        "mealTypes": meal_types,
        "servings": servings,
        "dietaryNeeds": dietary_needs.strip(),
        "budgetLevel": budget_level,
        "mealPrepDays": meal_prep_days,
    }
    _persist_user(request)
    clear_clusters(request)
    return RedirectResponse("/mode", status_code=303)


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
    saved = request.session.get("cuisinePreferences", {})
    return templates.TemplateResponse("cuisine.html", {
        "request": request,
        "preferences": get_prefs(request),
        "cuisines": CUISINES,
        "seasons": SEASONS,
        "selected_cuisines": saved.get("cuisines") or random.sample(CUISINES, 3),
        "selected_season": saved.get("season", ""),
    })


@app.post("/cuisine")
async def save_cuisine(
    request: Request,
    cuisines: List[str] = Form(default=[]),
    season: str = Form(""),
):
    request.session["cuisinePreferences"] = {"cuisines": cuisines, "season": season}
    clear_clusters(request)
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
    meal_types: list = prefs.get("mealTypes", [])
    mode = request.session.get("mode", "suggestions")
    existing = request.session.get("existingIngredients", [])
    sets_per_type = _sets_per_type(prefs)
    has_clusters_by_mt = {
        mt: any(
            not c.get("reserve") and not c.get("skipped") and c.get("mealType") == mt
            for c in clusters
        )
        for mt in meal_types
    }
    return templates.TemplateResponse(
        "suggest.html",
        {
            "request": request,
            "preferences": prefs,
            "clusters": clusters,
            "has_clusters_by_mt": has_clusters_by_mt,
            "can_view_plan": _can_view_plan(clusters, meal_types, sets_per_type),
            "meal_types": meal_types,
            "mode": mode,
            "existing_ingredients": existing,
            "sets_per_type": sets_per_type,
        },
    )


@app.post("/generate/{meal_type}", response_class=HTMLResponse)
async def generate_suggestions(request: Request, meal_type: str):
    prefs = request.session.get("preferences")
    if not prefs:
        return HTMLResponse("<p class='error-msg'>Session expired. <a href='/'>Start over</a></p>")

    allowed_meal_types = {"breakfast", "lunch", "dinner"}
    if meal_type not in allowed_meal_types:
        return HTMLResponse("")

    servings: int = prefs.get("servings", 2)
    dietary_needs: str = prefs.get("dietaryNeeds", "")
    budget_level: str = prefs.get("budgetLevel", "moderate")
    meal_prep_days: list = prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"])
    mode: str = request.session.get("mode", "suggestions")

    n_days = len(meal_prep_days) if meal_prep_days else 5

    if n_days >= 5:
        n_shown = 2
        c1_size = math.ceil(n_days / 2)   # e.g. 5→3, 6→3, 7→4
        c2_size = math.floor(n_days / 2)  # e.g. 5→2, 6→3, 7→3
    else:
        n_shown = 1
        c1_size = max(1, n_days)
        c2_size = 0

    n_total_clusters = n_shown * 2  # shown + same number of reserve backups

    if n_shown == 2:
        cluster_sizes_note = (
            f"Cluster 1 must have exactly {c1_size} meals. "
            f"Cluster 2 must have exactly {c2_size} meals. "
            f"Together they cover all {n_days} meal prep days with no repeats."
        )
    else:
        cluster_sizes_note = (
            f"Each cluster must have exactly {c1_size} meal{'s' if c1_size != 1 else ''} "
            f"covering all {n_days} meal prep day{'s' if n_days != 1 else ''}."
        )

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
    if mode == "suggestions":
        cp = request.session.get("cuisinePreferences", {})
        selected_cuisines = cp.get("cuisines", [])
        season = cp.get("season", "")
        if selected_cuisines:
            cuisine_note += f"\nCuisine style: {', '.join(selected_cuisines)}."
        if season and season in _SEASON_GUIDANCE:
            cuisine_note += f"\nSeason: {season}. Lean into {_SEASON_GUIDANCE[season]}"
        for cuisine in selected_cuisines:
            if cuisine in _CUISINE_RECIPE_LIBRARY:
                recipes = _CUISINE_RECIPE_LIBRARY[cuisine]
                cuisine_note += f"\n{cuisine} featured recipes ] strongly prefer these when suggesting {cuisine} meals:\n"
                cuisine_note += "\n".join(f"  - {r}" for r in recipes)

    if budget_level == "budget":
        adventure_note = "\nBudget: keep ingredients minimal and affordable [ skip optional garnishes, specialty items, and pricier add-ons. Use pantry staples and everyday proteins."
    elif budget_level == "flexible":
        adventure_note = "\nBudget: flexible ] feel free to include specialty ingredients, premium proteins, and optional extras."
    else:
        adventure_note = "\nBudget: moderate [ include all core ingredients but note which extras are optional."

    favorite_ids_pref: list = prefs.get("favorited_recipes", [])
    fav_titles = [
        r["base"]["title"] for r in RECIPE_DB
        if r["_id"] in favorite_ids_pref
        and meal_type in _RECIPE_MEAL_TYPES.get(r["_id"], [])
    ]
    if fav_titles:
        if budget_level == "budget":
            favorites_note = (
                f"\nFavorites: The user has saved these recipes ] since they're on a budget, "
                f"prioritize these familiar favorites heavily: {', '.join(fav_titles)}."
            )
        elif budget_level == "flexible":
            favorites_note = (
                f"\nFavorites: The user has saved these recipes, but feel free to explore new ideas too: "
                f"{', '.join(fav_titles)}."
            )
        else:
            favorites_note = (
                f"\nFavorites: Try to include at least one of these saved recipes alongside new suggestions: "
                f"{', '.join(fav_titles)}."
            )
    else:
        favorites_note = ""

    if meal_type != "breakfast":
        library_note = """
Some featured recipes to draw from when they fit the user's preferences (feel free to also suggest other meals alongside these):
Proteins: Baked Harissa Salmon, Baked Salmon with Pistachio Pesto, Crispy Turmeric Salmon with Yogurt Sauce, Garlic Shrimp with Smoked Paprika & Honey, Honey Mustard Garlic Shrimp, Baked Miso Maple Tofu, Herby Parmesan Meatballs, Hot Honey Zaatar Turkey Sausage, Miso Maple Chili Crisp Chicken
Sides: Crispy Roasted Potatoes with Chili & Paprika, Honey-Roasted Broccolini & Kale, Curry-Roasted Cauliflower with Minty Yogurt, Roasted Butternut Squash with Kale & Coconut Cream, Garlic Sauteed Green Beans with Dijon Vinaigrette, Roasted Zucchini with Parmesan & Basil
Sauce: Miso Tahini Sauce
You can name a cluster after one of these proteins and pair it with the sides [ but also freely suggest other meals that are not on this list."""
    else:
        library_note = ""

    # Breakfast gets food-specific guidance regardless of adventure level
    if meal_type == "breakfast":
        breakfast_note = """
Breakfast clusters must be actual breakfast foods. Good options: granola and yogurt bowls (parfaits), overnight oats with toppings, smoothie bowls, avocado toast and egg toasts, muffins or banana bread, eggs any style (scrambled, fried, poached), omelettes, frittatas, shakshuka, breakfast burritos, French toast, pancakes or waffles. Each cluster should share a prep-ahead base (e.g. a baked egg base, a batch of granola, a muffin batter, an oat base)."""
    else:
        breakfast_note = ""

    # Avoid repeating proteins already planned for other meal types this week
    other_type_clusters = [
        c for c in load_clusters(request)
        if c.get("mealType") != meal_type and not c.get("reserve")
    ]
    other_meal_names = []
    for c in other_type_clusters:
        for m in c.get("meals", []):
            if m.get("name"):
                other_meal_names.append(m["name"])
    if other_meal_names:
        other_meal_note = (
            f"\nIMPORTANT: The following specific recipes are already planned for other meals this week: "
            f"{', '.join(other_meal_names)}. Do NOT suggest any of these exact recipes for {meal_type}. "
            f"You may use the same proteins but with different preparations, sauces, or styles."
        )
    else:
        other_meal_note = ""

    system_prompt = (
        "You are a meal planning assistant. Each cluster is a single meal-prep session: "
        "one protein or base cooked once, eaten different ways across the week. "
        "Never mix proteins within a cluster. Return ONLY valid JSON ] no markdown fences, no explanation."
    )

    user_prompt = f"""Create {meal_type} suggestions for a weekly meal plan.

User:
- Servings: {servings} people
- Dietary restrictions: {dietary_needs or 'none'}{adventure_note}
- Meal prep days: {', '.join(meal_prep_days)}
{existing_note}{cuisine_note}{favorites_note}{library_note}{breakfast_note}{other_meal_note}

Rules:
- Suggest exactly {n_total_clusters} clusters (clusters {n_shown + 1}-{n_total_clusters} are hidden backup alternatives matching the same meal-count structure)
- {cluster_sizes_note}
- CRITICAL [ each cluster must have ONE single protein or base that is cooked ONCE during meal prep. Every meal in the cluster is a different way to serve that same cooked base. Never mix proteins within a cluster (e.g. no beef + chicken in the same cluster). The clusterName should be the protein/base itself (e.g. "Ground Beef", "Roasted Salmon", "Baked Tofu").
- The first {n_shown} cluster(s) cover the meal prep days: cluster 1 uses a fresh/perishable protein, cluster 2 (if present) uses a different protein ] never the same one as cluster 1
- Plain descriptive names only [ no puns or marketing language
- Ingredient amounts for exactly {servings} person/people per meal. Real portions: protein 4-6 oz, grains 1/2 cup dry, eggs 2 per person.
- Keep ingredient names consistent and specific where it matters (e.g. "cherry tomatoes", "red bell pepper", "poblano peppers" are fine). Avoid redundant qualifiers: write "parsley" not "fresh flat-leaf parsley", "ginger" not "fresh ginger", "garlic" not "garlic cloves". Never repeat the same ingredient under two slightly different names.
- suggestedDay: one day abbreviation e.g. "Mon"
- category: exactly one of "Produce", "Meat & Seafood", "Dairy & Eggs", "Bakery", "Pantry", "Frozen"

Return this exact JSON (no markdown):
{{
  "clusters": [
    {{
      "id": "short_unique_id",
      "clusterName": "Plain descriptive name",
      "tagline": "One-line description",
      "meals": [{{"name": "Meal Name", "description": "One sentence.", "suggestedDay": "Mon"}}],
      "ingredients": [{{"name": "ingredient", "amount": "2", "unit": "cups", "userHas": false, "category": "Produce"}}],
      "difficulty": "Easy"
    }}
  ]
}}"""

    try:
        client = get_anthropic_client()
        message = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = message.content[0].text
        json_match = re.search(r"\{[\s\S]*\}", raw)
        if not json_match:
            raise ValueError("No JSON found in response")

        json_str = re.sub(r",\s*([}\]])", r"\1", json_match.group(0))
        data = json.loads(json_str)
        new_clusters = data.get("clusters", [])

        type_count = 0
        for cluster in new_clusters:
            cluster["mealType"] = meal_type
            cluster["accepted"] = False
            cluster["skipped"] = False
            if not cluster.get("id"):
                cluster["id"] = str(uuid.uuid4())[:8]
            type_count += 1
            cluster["reserve"] = (type_count > n_shown)

        # Merge into the shared store safely
        sk = _store_key(request)
        async with _get_cluster_lock(sk):
            existing_clusters = load_clusters(request)
            # Remove any old clusters for this meal type, then append fresh ones
            kept = [c for c in existing_clusters if c.get("mealType") != meal_type]
            save_clusters(request, kept + new_clusters)

        return templates.TemplateResponse(
            "partials/meal_type_section.html",
            {
                "request": request,
                "meal_type": meal_type,
                "clusters": new_clusters,
                "mode": mode,
            },
        )

    except Exception as exc:
        return templates.TemplateResponse(
            "partials/generate_error.html",
            {"request": request, "error": str(exc)},
        )


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
        # Store was wiped (e.g. server restart) ] prompt reload
        return HTMLResponse(
            f'<div id="cluster-{cluster_id}" class="cluster-card" '
            f'style="padding:20px;text-align:center;">'
            f'<p style="color:var(--text-3);font-size:.875rem;margin-bottom:12px;">Session expired.</p>'
            f'<a href="/suggest" class="btn btn-primary btn-sm">Reload suggestions</a>'
            f'</div>'
        )

    mode = request.session.get("mode", "suggestions")
    return templates.TemplateResponse(
        "partials/cluster_card.html",
        {"request": request, "cluster": target, "mode": mode},
    )


@app.post("/skip/{cluster_id}", response_class=HTMLResponse)
async def skip_cluster(request: Request, cluster_id: str):
    sk = _store_key(request)
    replacement = None
    async with _get_cluster_lock(sk):
        clusters = load_clusters(request)
        target = next((c for c in clusters if c["id"] == cluster_id), None)
        if not target:
            return HTMLResponse(f'<div id="cluster-{cluster_id}" style="display:none"></div>')

        meal_type = target["mealType"]
        target["skipped"] = True

        # Try next queued reserve first
        replacement = next(
            (c for c in clusters if
             c.get("mealType") == meal_type and
             c.get("reserve") and
             not c.get("skipped")),
            None
        )
        if replacement:
            replacement["reserve"] = False
        else:
            # All reserves used [ loop back to oldest skipped non-accepted cluster
            replacement = next(
                (c for c in clusters if
                 c.get("mealType") == meal_type and
                 c.get("skipped") and
                 not c.get("accepted") and
                 c["id"] != cluster_id),
                None
            )
            if replacement:
                replacement["skipped"] = False
                replacement["reserve"] = False

        save_clusters(request, clusters)

    mode = request.session.get("mode", "suggestions")
    if replacement:
        return templates.TemplateResponse(
            "partials/cluster_card.html",
            {"request": request, "cluster": replacement, "mode": mode},
        )
    return HTMLResponse(
        f'<div id="cluster-{cluster_id}" style="display:none;height:0;margin:0;padding:0;overflow:hidden;"></div>'
    )


_CAT_ORDER = ["Produce", "Meat & Seafood", "Dairy & Eggs", "Bakery", "Frozen", "Pantry"]
_VALID_CATS = set(_CAT_ORDER)


def _build_grocery_by_recipe(accepted: list) -> list:
    """Return ingredients grouped by cluster for the 'by recipe' view."""
    result = []
    for cluster in accepted:
        items = []
        for ing in cluster.get("ingredients", []):
            if not ing.get("userHas", False):
                items.append({**ing, "key": _normalize_ingredient(ing.get("name", ""))})
        if items:
            result.append({
                "name": cluster.get("clusterName", ""),
                "meal_type": cluster.get("mealType", "dinner"),
                "items": items,
            })
    return result


def _build_grocery_data(accepted: list, meal_prep_days: list) -> tuple:
    """Return (raw_items, grocery_by_category) for a list of accepted clusters."""
    _, meal_counts = _build_calendar(accepted, meal_prep_days)
    grocery_map: dict = {}
    for cluster in accepted:
        cluster_meals = [m for m in cluster.get("meals", []) if m.get("name")]
        n_meals = len(cluster_meals)
        scale = max(1, round(sum(meal_counts.get((cluster["id"], m["name"]), 1) for m in cluster_meals) / n_meals)) if n_meals else 1
        for ing in cluster.get("ingredients", []):
            if not ing.get("userHas", False):
                key = _normalize_ingredient(ing["name"])
                if key not in grocery_map:
                    grocery_map[key] = {**ing, "name": key, "key": key,
                                        "amount": _scale_amount(str(ing.get("amount", "")), scale)}
    raw_items = list(grocery_map.values())
    by_cat: dict = {}
    for item in raw_items:
        cat = item.get("category", "Pantry") if item.get("category", "Pantry") in _VALID_CATS else "Pantry"
        by_cat.setdefault(cat, []).append(item)
    return raw_items, {c: by_cat[c] for c in _CAT_ORDER if c in by_cat}


@app.get("/plan", response_class=HTMLResponse)
async def plan_page(request: Request):
    clusters = load_clusters(request)
    accepted = [c for c in clusters if c.get("accepted")]
    if not accepted:
        return RedirectResponse("/suggest")

    prefs = get_prefs(request)
    meal_types: list = prefs.get("mealTypes", [])
    meal_prep_days: list = prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"])

    # Save this plan under the current selected week so /grocery can show it
    today = date.today()
    selected_week = request.session.get("selectedWeek", _week_key(today))
    plans_by_week: dict = request.session.get("plans_by_week", {})
    plans_by_week[selected_week] = {
        "clusters": accepted,
        "meal_types": meal_types,
        "meal_prep_days": meal_prep_days,
    }
    request.session["plans_by_week"] = plans_by_week
    _persist_user(request)

    checked: List[str] = request.session.get(f"checked_{selected_week}", [])
    calendar, _ = _build_calendar(accepted, meal_prep_days)
    raw_items, grocery_by_category = _build_grocery_data(accepted, meal_prep_days)
    grocery_by_recipe = _build_grocery_by_recipe(accepted)

    return templates.TemplateResponse(
        "plan.html",
        {
            "request": request,
            "preferences": prefs,
            "meal_types": meal_types,
            "accepted_clusters": accepted,
            "calendar": calendar,
            "grocery_list": raw_items,
            "grocery_by_category": grocery_by_category,
            "grocery_by_recipe": grocery_by_recipe,
            "checked_items": checked,
            "week_key": selected_week,
        },
    )


@app.get("/browse", response_class=HTMLResponse)
async def browse_recipes(request: Request):
    prefs = get_prefs(request)
    favorite_ids: list = prefs.get("favorited_recipes", [])
    selected_meal_types: list = prefs.get("mealTypes", ["breakfast", "lunch", "dinner"])

    groups: dict[str, list] = {"breakfast": [], "lunch": [], "dinner": []}
    cuisine_groups: dict[str, list] = {"Italian": [], "Mexican": [], "Japanese": [], "Chinese": [], "American": [], "French": [], "Korean": [], "Mediterranean": [], "Indian": [], "Spanish": [], "Vietnamese": []}
    all_entries: list = []
    recipe_data: dict = {}

    for recipe in RECIPE_DB:
        rid = recipe["_id"]
        meal_types = _RECIPE_MEAL_TYPES.get(rid, ["lunch", "dinner"])
        cuisine = _RECIPE_CUISINE.get(rid)
        entry = {
            "id": rid,
            "image": recipe.get("image", ""),
            "title": recipe["base"]["title"],
            "intro": recipe.get("intro", ""),
            "search_text": " ".join([
                recipe["base"]["title"],
                recipe.get("intro", ""),
                " ".join(recipe.get("_keywords", [])),
                " ".join(u["name"] for u in recipe.get("uses", [])),
            ]).lower(),
            "uses_names": [u["name"] for u in recipe.get("uses", [])],
            "meal_types": meal_types,
            "cuisine": cuisine,
        }
        recipe_data[rid] = {
            "id": rid,
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
    return JSONResponse({"favorited": is_fav, "id": recipe_id})


@app.get("/grocery", response_class=HTMLResponse)
async def grocery_page(request: Request):
    prefs = get_prefs(request)
    plans_by_week: dict = request.session.get("plans_by_week", {})

    weeks_data = []
    for week_key in sorted(plans_by_week.keys(), reverse=True):
        plan = plans_by_week[week_key]
        accepted = plan.get("clusters", [])
        if not accepted:
            continue
        meal_prep_days = plan.get("meal_prep_days", prefs.get("mealPrepDays", ["Mon","Tue","Wed","Thu","Fri"]))
        week_dates = _week_dates(week_key)
        week_range = f"{week_dates[0].strftime('%b %-d')} – {week_dates[6].strftime('%b %-d')}"
        raw_items, grocery_by_category = _build_grocery_data(accepted, meal_prep_days)
        grocery_by_recipe = _build_grocery_by_recipe(accepted)
        checked = request.session.get(f"checked_{week_key}", [])
        weeks_data.append({
            "week_key": week_key,
            "week_range": week_range,
            "grocery_list": raw_items,
            "grocery_by_category": grocery_by_category,
            "grocery_by_recipe": grocery_by_recipe,
            "checked_items": checked,
        })

    active_week = request.query_params.get("week", weeks_data[0]["week_key"] if weeks_data else "")
    return templates.TemplateResponse(
        "grocery.html",
        {
            "request": request,
            "preferences": prefs,
            "weeks": weeks_data,
            "active_week": active_week,
        },
    )


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


@app.get("/recipes", response_class=HTMLResponse)
async def recipes_page(request: Request):
    clusters = load_clusters(request)
    _mt_order = {"breakfast": 0, "lunch": 1, "dinner": 2}
    accepted = sorted(
        [c for c in clusters if c.get("accepted")],
        key=lambda c: _mt_order.get(c.get("mealType", ""), 99),
    )
    if not accepted:
        return RedirectResponse("/suggest")

    sk = _store_key(request)
    cached = _recipe_store.get(sk)
    if not cached:
        cached = await _generate_recipes(accepted, request)
        _recipe_store[sk] = cached

    return templates.TemplateResponse(
        "recipes.html",
        {
            "request": request,
            "preferences": get_prefs(request),
            "recipe_clusters": cached,
        },
    )


@app.post("/recipes/regenerate")
async def regenerate_recipes(request: Request):
    sk = _store_key(request)
    _recipe_store.pop(sk, None)
    return RedirectResponse("/recipes", status_code=303)


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


async def _ai_recipe(client, cluster_summary: dict, servings: int, dietary_note: str, other_bases: list[str]) -> dict | None:
    """Generate a recipe via AI. other_bases lists base titles already used ] avoid repeating them."""
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

    message = await asyncio.to_thread(
        client.messages.create,
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        return None
    json_str = re.sub(r",\s*([}\]])", r"\1", json_match.group(0))
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


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

    client = get_anthropic_client()

    # Phase 1: sequential DB matching [ each DB entry used at most once
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
        _ai_recipe(client, cs, servings, dietary_note, db_bases)
        for _, cs in ai_items
    ])

    # Merge back in original order
    ai_iter = iter(ai_results)
    final = []
    for kind, data in plan:
        if kind == "db":
            final.append(data)
        else:
            r = next(ai_iter)
            if r:
                final.append(r)

    return final


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not request.session.get("preferences"):
        return RedirectResponse("/landing")

    today = date.today()
    current_week = _week_key(today)
    selected_week = request.session.get("selectedWeek", current_week)
    try:
        week_dates_list = _week_dates(selected_week)
    except Exception:
        selected_week = current_week
        week_dates_list = _week_dates(selected_week)

    # Calendar month to display ] default to month of selected week's Monday
    cal_year = int(request.session.get("calYear", week_dates_list[0].year))
    cal_month = int(request.session.get("calMonth", week_dates_list[0].month))
    month_weeks = _month_weeks(cal_year, cal_month)

    clusters = load_clusters(request)
    accepted = [c for c in clusters if c.get("accepted")]
    prefs = get_prefs(request)
    meal_types = prefs.get("mealTypes", [])
    meal_prep_days = prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"])
    day_completions = request.session.get("dayCompletions", {})

    calendar_data = []
    if accepted:
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

    # Formatted week range for display
    mon = week_dates_list[0]
    sun = week_dates_list[6]
    if mon.month == sun.month:
        week_range = f"{mon.strftime('%b')} {mon.day}–{sun.day}, {mon.year}"
    else:
        week_range = f"{mon.strftime('%b')} {mon.day} – {sun.strftime('%b')} {sun.day}, {mon.year}"

    prev_month = cal_month - 1 if cal_month > 1 else 12
    prev_year = cal_year if cal_month > 1 else cal_year - 1
    next_month = cal_month + 1 if cal_month < 12 else 1
    next_year = cal_year if cal_month < 12 else cal_year + 1

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "preferences": prefs,
        "today": today,
        "today_str": today.isoformat(),
        "selected_week": selected_week,
        "current_week": current_week,
        "week_range": week_range,
        "cal_year": cal_year,
        "cal_month": cal_month,
        "cal_month_name": _MONTH_NAMES[cal_month - 1],
        "month_weeks": month_weeks,
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
        "calendar_data": calendar_data,
        "has_plan": bool(accepted),
        "meal_types": meal_types,
    })


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
    return RedirectResponse("/mode")


@app.get("/reset")
async def reset(request: Request):
    clear_clusters(request)
    request.session.clear()
    return RedirectResponse("/")
