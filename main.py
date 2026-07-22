from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import anthropic
import asyncio
import calendar as _cal
import json
import math
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
    "Summer": "seasonal summer produce (tomatoes, zucchini, corn, peppers, basil, stone fruits, cucumbers, berries). Fresh, lighter meals — grilling works well.",
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
        "More interesting recipes but still accessible ingredients — things any grocery store stocks: "
        "dijon mustard, capers, sun-dried tomatoes, fresh herbs, greek yogurt, sriracha, rice vinegar, "
        "tahini, smoked paprika, cumin, feta. No specialty imports or hard-to-source items."
    ),
    "bold": (
        "Complex recipes with specialty and global ingredients: mirin, fish sauce, miso, harissa, cotija, "
        "nori, gochujang, preserved lemon, sumac, za'atar, pomegranate molasses, tamarind, etc. "
        "Explore lesser-known cuisines and advanced techniques."
    ),
}

# ── Recipe database (Good Mood Food newsletter) ──────────────────────────────
# Each entry has private _id and _keywords fields (stripped before returning),
# plus the exact schema that _generate_one_recipe() returns.
RECIPE_DB = [
    {
        "_id": "miso-maple-chicken",
        "_keywords": ["chicken", "miso"],
        "intro": "One sticky, savory marinade does all the work — cook the chicken and vegetables together, then eat from it three different ways across the week.",
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
                "tip": "Kale holds up well in the fridge — this is a great pack-ahead lunch.",
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
        "intro": "Bold harissa paste does all the work — marinate and bake, then pair the salmon with completely different sides each day.",
        "base": {
            "title": "Baked Harissa Salmon",
            "ingredients": [
                "4 salmon fillets (4-6 oz each)",
                "1/4 cup mild harissa paste",
                "2 cloves garlic, minced",
                "2 tbsp olive oil",
                "Salt and pepper",
            ],
            "steps": [
                "Preheat oven to 400 F.",
                "Whisk together harissa paste, garlic, and olive oil in a small bowl.",
                "Place salmon fillets in a baking dish and coat with the harissa mixture.",
                "Bake for 15 minutes until cooked through.",
            ],
        },
        "uses": [
            {
                "name": "Lemon Quinoa Bowl",
                "subtitle": "with wilted kale, golden raisins, and harissa salmon",
                "extras": [
                    "1/2 cup dry quinoa",
                    "4 cups kale, shredded or torn",
                    "Juice and zest of 1 lemon",
                    "3 tbsp golden raisins",
                    "1 tbsp olive oil",
                ],
                "steps": [
                    "Cook quinoa according to package directions.",
                    "Heat olive oil in a pan over medium heat and saute kale for about 2 minutes until wilted.",
                    "Stir kale into the quinoa with lemon juice, zest, and raisins. Season with salt and pepper.",
                    "Spoon into bowls and top with harissa salmon.",
                ],
                "tip": None,
            },
            {
                "name": "Honey Broccolini Plate",
                "subtitle": "with miso tahini drizzle and crispy kale",
                "extras": [
                    "2 bunches broccolini, ends trimmed",
                    "4 cups curly kale, torn",
                    "2 tsp honey",
                    "For miso tahini: 1/4 cup tahini, 2 tbsp miso paste, 6 tbsp nutritional yeast, 1/4 tsp garlic powder, 6 tbsp hot water",
                ],
                "steps": [
                    "Toss broccolini with olive oil, salt, and pepper. Roast at 350 F for 15 minutes.",
                    "Add kale, drizzle with olive oil, and roast 5 more minutes. Finish with honey.",
                    "Blend miso tahini: combine tahini, miso, nutritional yeast, garlic powder, and hot water until smooth.",
                    "Plate salmon over the broccolini and kale. Drizzle miso tahini generously over everything.",
                ],
                "tip": None,
            },
            {
                "name": "Green Bean Salad",
                "subtitle": "with Dijon vinaigrette, toasted almonds, and fresh parsley",
                "extras": [
                    "1-1.5 lbs green beans, ends trimmed",
                    "2 cloves garlic, minced",
                    "1/2 cup almonds, chopped",
                    "1/4 cup fresh parsley, chopped",
                    "For vinaigrette: 1/4 cup olive oil, 1 tbsp white wine vinegar, 3 tsp Dijon, 1/2 tsp garlic powder, 1 tbsp lemon juice",
                ],
                "steps": [
                    "Shake all vinaigrette ingredients together in a jar.",
                    "Saute green beans with olive oil and garlic over medium heat for 10-12 minutes until tender-crisp.",
                    "Toast almonds in a dry pan 7-10 minutes until lightly golden.",
                    "Toss green beans with vinaigrette, almonds, and parsley. Lay salmon alongside and serve with lemon wedges.",
                ],
                "tip": None,
            },
        ],
    },
    {
        "_id": "turmeric-salmon",
        "_keywords": ["turmeric", "salmon"],
        "intro": "Pan-seared with a golden turmeric crust and served with a cool herbed yogurt — this salmon pairs beautifully with bold roasted vegetables.",
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
                "tip": "Reheat potatoes in the oven or a hot pan — microwave makes them soft.",
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
        "intro": "A bright, nutty pistachio pesto takes minutes to blend and turns a simple baked salmon into something that feels special — mix with different sides all week.",
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
                "tip": "Pesto keeps in the fridge for a week — spoon it on toast, pasta, or grain bowls all week.",
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
        "intro": "Simple lemon-baked salmon that splits into three completely different meals — a vibrant Mediterranean bowl, spicy hand rolls, and fresh fish tacos.",
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
                "tip": "Assemble these just before eating — nori gets soggy fast.",
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
        "intro": "A big batch of herby, Parmesan-loaded meatballs — bake them once and use them three completely different ways through the week.",
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
                "Combine all ingredients in a large bowl, mixing gently — do not overwork the meat.",
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
        "_id": "overnight-oats",
        "_keywords": ["oats"],
        "intro": "Five minutes of prep the night before means breakfast is already waiting — make a batch on Sunday and vary the toppings all week.",
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
        "intro": "Massaged kale stays fresh and hearty all week — one big batch of greens and carrot ginger dressing turns into a wrap, a grain bowl, and a warm pasta.",
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
                "Toss in the grated carrot and beet if using. Store undressed in the fridge — it holds up to 4 days.",
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
                "tip": "Pack the dressing on the side if making these ahead — it keeps the wrap from getting soggy.",
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
        "intro": "A 15-minute shrimp base that pairs with three completely different vegetable sides — fast enough for any weeknight, varied enough for the whole week.",
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
                "Heat a pan over medium-high until very hot — a drop of water should sizzle immediately.",
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
        "intro": "Crispy miso-maple tofu is the weeknight protein that makes vegetables the main event — three bold pairings, all built around the same golden base.",
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
                "tip": "Make extra minty yogurt — it keeps 3-4 days and is great on everything.",
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
        "intro": "A Middle Eastern-spiced turkey sausage with hot honey and zaatar — cook it once and it turns into a grain bowl, a creamy pasta, and a breakfast frittata.",
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
                "tip": "Check pasta at 10 minutes — cooking time varies by shape.",
            },
            {
                "name": "Potato and Goat Cheese Frittata",
                "subtitle": "with kale, gold potatoes, and turkey sausage",
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


# ── Routes ──────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(url="/landing", status_code=302)


@app.get("/landing", response_class=HTMLResponse)
async def landing_page(request: Request):
    prefs = request.session.get("preferences")
    return templates.TemplateResponse("landing.html", {
        "request": request,
        "has_plan": bool(prefs and load_clusters(request)),
        "has_prefs": bool(prefs),
    })


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "preferences": {}})


@app.post("/setup")
async def setup(
    request: Request,
    meal_types: List[str] = Form(default=[]),
    servings: int = Form(2),
    dietary_needs: str = Form(""),
    adventure_level: str = Form("curious"),
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
        "adventureLevel": adventure_level,
        "mealPrepDays": meal_prep_days,
    }
    return RedirectResponse("/mode", status_code=303)


@app.post("/update-settings")
async def update_settings(
    request: Request,
    meal_types: List[str] = Form(default=[]),
    servings: int = Form(2),
    dietary_needs: str = Form(""),
    adventure_level: str = Form("curious"),
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
        "adventureLevel": adventure_level,
        "mealPrepDays": meal_prep_days,
    }
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
    adventure_level: str = prefs.get("adventureLevel", "curious")
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

    adventure_note = f"\nFlavor profile: {_ADVENTURE_GUIDANCE.get(adventure_level, _ADVENTURE_GUIDANCE['curious'])}"

    # Library recipes only appear at curious/bold — familiar gets simple everyday cooking
    if adventure_level in ("curious", "bold") and meal_type != "breakfast":
        library_note = """
Some featured recipes to draw from when they fit the user's preferences (feel free to also suggest other meals alongside these):
Proteins: Baked Harissa Salmon, Baked Salmon with Pistachio Pesto, Crispy Turmeric Salmon with Yogurt Sauce, Garlic Shrimp with Smoked Paprika & Honey, Honey Mustard Garlic Shrimp, Baked Miso Maple Tofu, Herby Parmesan Meatballs, Hot Honey Zaatar Turkey Sausage, Miso Maple Chili Crisp Chicken
Sides: Crispy Roasted Potatoes with Chili & Paprika, Honey-Roasted Broccolini & Kale, Curry-Roasted Cauliflower with Minty Yogurt, Roasted Butternut Squash with Kale & Coconut Cream, Garlic Sauteed Green Beans with Dijon Vinaigrette, Roasted Zucchini with Parmesan & Basil
Sauce: Miso Tahini Sauce
You can name a cluster after one of these proteins and pair it with the sides — but also freely suggest other meals that are not on this list."""
    elif adventure_level == "familiar":
        library_note = "\nStick to simple, classic, everyday cooking: roast chicken, pasta, stir-fry, soups, salads, rice bowls with familiar proteins. Nothing too niche or technique-heavy."
    else:
        library_note = ""

    # Breakfast gets food-specific guidance regardless of adventure level
    if meal_type == "breakfast":
        breakfast_note = """
Breakfast clusters must be actual breakfast foods. Good options: granola and yogurt bowls (parfaits), overnight oats with toppings, smoothie bowls, avocado toast and egg toasts, muffins or banana bread, eggs any style (scrambled, fried, poached), omelettes, frittatas, shakshuka, breakfast burritos, French toast, pancakes or waffles. Each cluster should share a prep-ahead base (e.g. a baked egg base, a batch of granola, a muffin batter, an oat base)."""
    else:
        breakfast_note = ""

    system_prompt = (
        "You are a meal planning assistant. Each cluster is a single meal-prep session: "
        "one protein or base cooked once, eaten different ways across the week. "
        "Never mix proteins within a cluster. Return ONLY valid JSON — no markdown fences, no explanation."
    )

    user_prompt = f"""Create {meal_type} suggestions for a weekly meal plan.

User:
- Servings: {servings} people
- Dietary restrictions: {dietary_needs or 'none'}{adventure_note}
- Meal prep days: {', '.join(meal_prep_days)}
{existing_note}{cuisine_note}{library_note}{breakfast_note}

Rules:
- Suggest exactly {n_total_clusters} clusters (clusters {n_shown + 1}-{n_total_clusters} are hidden backup alternatives matching the same meal-count structure)
- {cluster_sizes_note}
- CRITICAL — each cluster must have ONE single protein or base that is cooked ONCE during meal prep. Every meal in the cluster is a different way to serve that same cooked base. Never mix proteins within a cluster (e.g. no beef + chicken in the same cluster). The clusterName should be the protein/base itself (e.g. "Ground Beef", "Roasted Salmon", "Baked Tofu").
- The first {n_shown} cluster(s) cover the meal prep days: cluster 1 uses a fresh/perishable protein, cluster 2 (if present) uses a different protein — never the same one as cluster 1
- Plain descriptive names only — no puns or marketing language
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
        # Store was wiped (e.g. server restart) — prompt reload
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
            # All reserves used — loop back to oldest skipped non-accepted cluster
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


@app.get("/plan", response_class=HTMLResponse)
async def plan_page(request: Request):
    clusters = load_clusters(request)
    accepted = [c for c in clusters if c.get("accepted")]
    if not accepted:
        return RedirectResponse("/suggest")

    prefs = get_prefs(request)
    meal_types: list = prefs.get("mealTypes", [])
    meal_prep_days: list = prefs.get("mealPrepDays", ["Mon", "Tue", "Wed", "Thu", "Fri"])
    checked: List[str] = request.session.get("checkedGrocery", [])

    calendar, meal_counts = _build_calendar(accepted, meal_prep_days)

    # Scale ingredient amounts by how many times the cluster's meals repeat in the week.
    # A cluster with 2 meals each appearing twice is cooked "2x", so all its amounts double.
    grocery_map: dict = {}
    for cluster in accepted:
        cluster_meals = [m for m in cluster.get("meals", []) if m.get("name")]
        n_meals = len(cluster_meals)
        if n_meals:
            total_appearances = sum(
                meal_counts.get((cluster["id"], m["name"]), 1) for m in cluster_meals
            )
            scale = max(1, round(total_appearances / n_meals))
        else:
            scale = 1

        for ing in cluster.get("ingredients", []):
            if not ing.get("userHas", False):
                key = _normalize_ingredient(ing["name"])
                if key not in grocery_map:
                    grocery_map[key] = {
                        **ing,
                        "name": key,
                        "key": key,
                        "amount": _scale_amount(str(ing.get("amount", "")), scale),
                    }

    # Group by grocery store section in a logical shopping order
    _cat_order = ["Produce", "Meat & Seafood", "Dairy & Eggs", "Bakery", "Frozen", "Pantry"]
    _valid_cats = set(_cat_order)
    raw_items = list(grocery_map.values())
    by_cat: dict = {}
    for item in raw_items:
        cat = item.get("category", "Pantry")
        if cat not in _valid_cats:
            cat = "Pantry"
        by_cat.setdefault(cat, []).append(item)
    grocery_by_category = {c: by_cat[c] for c in _cat_order if c in by_cat}

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
    """Generate a recipe via AI. other_bases lists base titles already used — avoid repeating them."""
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

    # Phase 1: sequential DB matching — each DB entry used at most once
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

    # Calendar month to display — default to month of selected week's Monday
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
):
    if re.match(r"^\d{4}-W\d{2}$", week):
        request.session["selectedWeek"] = week
    if cal_year:
        request.session["calYear"] = cal_year
    if cal_month:
        request.session["calMonth"] = cal_month
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/calendar-nav")
async def calendar_nav(
    request: Request,
    year: int = Form(...),
    month: int = Form(...),
):
    request.session["calYear"] = year
    request.session["calMonth"] = month
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/new-plan")
async def new_plan(request: Request):
    """Clear clusters but keep preferences — takes user back to choose mode."""
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
