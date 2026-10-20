"""Week/month helpers, calendar building and amount scaling."""

from datetime import date
from datetime import timedelta
import calendar as _cal
import re


# ── Calendar helpers ─────────────────────────────────────────────────────────
DAYS_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

DAYS_FULL  = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


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


def _scale_amount_f(amount: str, scale: float) -> str:
    """Multiply the leading number in an amount string by a float scale factor."""
    if not amount or abs(scale - 1.0) < 0.02:
        return amount
    s = amount.strip()
    m = re.match(r'^(\d+)\s*/\s*(\d+)(.*)', s)
    if m:
        val = int(m.group(1)) / int(m.group(2)) * scale
        rest = m.group(3).strip()
    else:
        m = re.match(r'^(\d+\.?\d*)(.*)', s)
        if not m:
            return s
        val = float(m.group(1)) * scale
        rest = m.group(2).strip()
    if val >= 10:
        fmt = str(int(round(val)))
    elif val >= 2:
        # Round to nearest 0.5 for practical measuring (2.7 tbsp → 3, 2.3 → 2.5)
        rounded = round(val * 2) / 2
        fmt = str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"
    elif val >= 0.5:
        # Round to nearest quarter (0.7 cups → 3/4)
        quarters = round(val * 4) / 4
        if quarters == int(quarters):
            fmt = str(int(quarters))
        elif quarters == 0.25:
            fmt = "1/4"
        elif quarters == 0.5:
            fmt = "1/2"
        elif quarters == 0.75:
            fmt = "3/4"
        elif quarters == 1.25:
            fmt = "1 1/4"
        elif quarters == 1.5:
            fmt = "1 1/2"
        elif quarters == 1.75:
            fmt = "1 3/4"
        else:
            fmt = f"{quarters:.2f}".rstrip('0').rstrip('.')
    else:
        fmt = f"{val:.2f}".rstrip('0').rstrip('.')
    return (fmt + (' ' + rest if rest else '')).strip()


def _build_calendar(accepted: list = None, meal_prep_days: list = None, *, planner: dict = None) -> tuple:  # noqa: ARG001
    """
    Returns (calendar, meal_counts).
    If planner dict provided, reads from it (new drag-and-drop model).
    Falls back to per-cluster dayAssignment for old data.
    """
    day_index = {d: i for i, d in enumerate(DAYS_SHORT)}
    slots: dict = {}
    meal_counts: dict = {}

    if planner is not None:
        # Build reverse map: meal_name → cluster_id for counting
        meal_cluster_map: dict = {}
        if accepted:
            for c in accepted:
                for m in c.get("meals", []):
                    if m.get("name"):
                        meal_cluster_map[m["name"]] = c["id"]
        for day, day_slots in planner.items():
            if day in day_index:
                for slot, meal_name in day_slots.items():
                    if meal_name:
                        slots.setdefault(day, {})[slot] = meal_name
                        cluster_id = meal_cluster_map.get(meal_name)
                        if cluster_id:
                            key = (cluster_id, meal_name)
                            meal_counts[key] = meal_counts.get(key, 0) + 1
    elif accepted:
        for c in accepted:
            for m in c.get("meals", []):
                if not m.get("name"):
                    continue
                assignment = m.get("dayAssignment") or {}
                day = assignment.get("day") or m.get("suggestedDay", "")
                slot = assignment.get("meal_slot") or m.get("mealType", "dinner")
                if day and day in day_index:
                    slots.setdefault(day, {})[slot] = m["name"]
                    meal_counts[(c["id"], m["name"])] = 1

    _slot_order = {"breakfast": 0, "lunch": 1, "dinner": 2}
    calendar = []
    for i, d in enumerate(DAYS_SHORT):
        if d in slots:
            ordered = dict(sorted(slots[d].items(), key=lambda x: _slot_order.get(x[0], 9)))
            calendar.append({"short": d, "full": DAYS_FULL[i], "meals": ordered})

    return calendar, meal_counts
