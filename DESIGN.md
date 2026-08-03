# Mise — Visual Direction

Reference images live in `design/inspo/`. Read them before writing any CSS.

## The thesis

Mise is a meal-planning app named after *mise en place*. The current UI is a flat
cream-and-terracotta web app that *references* a kitchen. The redesign should feel
like you are **standing in one** — a Mediterranean kitchen with hand-glazed tile,
reclaimed wood shelves, brass fixtures, and a notepad by the sink.

The differentiator is **material honesty**, not palette. Warm cream + terracotta is
the most common AI-design default there is; we already have it and it reads flat.
What makes this specific is that the surfaces behave like real glazed ceramic,
oiled wood, and soft plaster — irregular, reflective, slightly imperfect.

## What the references actually show

**Zellige tile (refs 1, 2, 5)** — The single most important material. Each tile is
a *different* color from its neighbor, even in a "monochrome" wall. Edges are
uneven and hand-cut. The glaze pools and puddles, so the specular highlight is
**off-center and irregular**, never a clean linear gradient. Grout is warm sand,
never white. Tiles are roughly 4:4 but not perfectly square.

**Wood shelving (refs 2, 3, 4)** — Thick, chunky, reclaimed. It casts a real drop
shadow onto the tile behind it. Objects sit *on* the shelf with their own contact
shadows. Ref 4 shows shelves crowded with real stuff: jars, dried flowers,
mismatched mugs on hooks.

**Hanging glassware (ref 3)** — Colored glasses suspended upside down from a dark
wood rail. Translucent, overlapping, each a different tint. Green, rose, amber,
cobalt, teal, smoke, clear.

**Plaster + light (refs 2, 5)** — Walls are soft warm putty with visible trowel
variation. Light falls across surfaces directionally, from a window on one side.

**Brass and black (refs 1, 4, 5)** — Fixture metal is aged brass or matte black.
Never chrome, never gray.

## Palette

Starting point. Sample the actual images and adjust — these are approximate.

| Token | Hex | Role |
|---|---|---|
| `--plaster` | `#EDE6DA` | page background, walls |
| `--plaster-deep` | `#DDD2C0` | recessed areas, shadow side |
| `--grout` | `#CFC0A8` | tile grout, hairline dividers |
| `--olive` | `#6E7B4F` | primary tile field, primary actions |
| `--sage` | `#A3B189` | lighter tile field (ref 4) |
| `--terra` | `#B5623C` | accent tile, CTAs |
| `--oxblood` | `#8E4A44` | accent tile, destructive |
| `--ochre` | `#C89442` | accent tile, highlights |
| `--slate` | `#7C8E93` | accent tile, muted state |
| `--cream-tile` | `#E4D6BC` | neutral tile field (ref 5) |
| `--walnut` | `#6B4C33` | shelves, header, rails |
| `--walnut-lt` | `#8A6540` | shelf top edge, wood highlight |
| `--brass` | `#A9843F` | fixtures, small metal details |
| `--ink` | `#2B2018` | body text |

Hard rule: **no pure white, no pure black, no gray anywhere.** Every neutral is
warm. `#FFFFFF` and `#000000` should not appear in the stylesheet.

## Type

- **Display** — `Cormorant Garamond` (already loaded). Headings, recipe titles.
  Use the italic for pull quotes and empty states.
- **Body** — replace the current system-font stack. Needs a warm humanist sans
  with real personality at small sizes. Propose one and justify it.
- **Hand** — `Caveat` (already loaded) for the grocery list, annotations, and
  anything meant to read as written by a person. Never for UI chrome.
- **Utility** — small caps, generous letterspacing, for shelf labels, category
  headers, and meal-type dividers. Can be the body face at low size.

Handwriting must never be used for anything the user has to read quickly or
precisely — quantities, prices, form labels, error text.

## Signature element

Pick **one** and make it excellent. Everything else stays quiet.

1. **The glass rail** — saved / favorited recipes hang as translucent colored
   glasses from a wood rail. Tints derive from cuisine or meal type.
2. **The tile wall** — the page background is a real zellige field; content cards
   sit on it like objects on a counter, with contact shadows.
3. **The shelf** — browse and plan views render as pottery and plates arranged on
   reclaimed wood shelves rather than a card grid.

State which you chose and why before building it.

## The grocery page

This page gets its own treatment and is the emotional center of the redesign.

It should read as **a notepad on the counter**, not a checklist component:

- Cream lined paper — ruled lines in faded blue, a single red margin rule
- Slight paper texture and a soft warp at the edges; it sits on the tile surface
  with a drop shadow, faintly rotated (0.4–0.8°)
- Item names in `Caveat`, varying subtly in size and baseline so no two lines are
  mechanically identical
- Category headers underlined by hand, not by a border rule
- Checking an item draws a pencil strike-through — a slightly wobbly path, not a
  straight `line-through`. Animate the stroke drawing left to right.
- Quantities in the margin, smaller, like they were added after
- Optional: a faint coffee ring, a bent corner, a paperclip on the tab strip

Quantities, totals, and the store selector stay in the utility face and stay
legible. The paper is the frame, not an excuse to make data hard to read.

## Motion

Restrained and physical. Tile gloss shifts slightly on hover, as if the light
moved. Glasses on the rail sway a few degrees. Paper lifts on interaction. No
fades-in-on-scroll, no parallax, no stagger animations on grids. Everything
respects `prefers-reduced-motion`.

## Quality floor

Responsive to 375px. Visible keyboard focus that suits the palette (brass ring,
not a browser default outline). Text contrast at least 4.5:1 against its actual
background — check this specifically where text sits on tile. Textures degrade
gracefully; nothing depends on an image file that might 404.
