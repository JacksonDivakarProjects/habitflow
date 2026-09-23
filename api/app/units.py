"""
Unit names and conversion.

Every stored unit goes through canonical() so "Mins", "min" and "minutes"
are one unit. Units in the same family convert, so totals can combine
"5 km" with "1 mile".
"""

from typing import Optional

_ALIASES = {
    "miles": ["mile", "miles", "mi"],
    "km": ["km", "kms", "kilometer", "kilometers", "kilometre", "kilometres"],
    # Bare "m" is ambiguous (meters or minutes): see AMBIGUOUS / resolve().
    "meters": ["meter", "meters", "metre", "metres"],
    "minutes": ["minute", "minutes", "min", "mins"],
    "hours": ["hour", "hours", "hr", "hrs", "h"],
    "seconds": ["second", "seconds", "sec", "secs"],
    "pages": ["page", "pages", "pg", "pgs"],
    "books": ["book", "books"],
    "chapters": ["chapter", "chapters", "ch"],
    "steps": ["step", "steps"],
    "reps": ["rep", "reps", "repetition", "repetitions"],
    "sets": ["set", "sets"],
    "concepts": ["concept", "concepts"],
    "glasses": ["glass", "glasses"],
    "liters": ["l", "liter", "liters", "litre", "litres"],
    "ml": ["ml", "milliliter", "milliliters", "millilitre", "millilitres"],
    "calories": ["cal", "cals", "calorie", "calories", "kcal"],
}
ALIASES = {alias: name for name, aliases in _ALIASES.items() for alias in aliases}

# Factor to the family's base unit.
_FAMILIES = {
    "distance": {"km": 1.0, "miles": 1.609344, "meters": 0.001},
    "time": {"minutes": 1.0, "hours": 60.0, "seconds": 1 / 60},
    "volume": {"liters": 1.0, "ml": 0.001},
}
_FAMILY_OF = {unit: fam for fam, units in _FAMILIES.items() for unit in units}


_SINGULAR = {
    "miles": "mile", "meters": "meter", "minutes": "minute", "hours": "hour",
    "seconds": "second", "pages": "page", "books": "book", "chapters": "chapter",
    "steps": "step", "reps": "rep", "sets": "set", "concepts": "concept",
    "glasses": "glass", "liters": "liter", "calories": "calorie", "laps": "lap",
}


def quantity(amount: float, unit: str) -> str:
    """'1 mile', '2 miles', '1.5 hours'. Units are stored plural; this is display only."""
    amount = float(amount)
    label = _SINGULAR.get(unit, unit) if amount == 1 else unit
    return f"{amount:g} {label}"


def canonical(unit: Optional[str]) -> Optional[str]:
    if unit is None:
        return None
    s = str(unit).strip().lower().rstrip(".")
    if not s:
        return None
    return ALIASES.get(s, s)


# Abbreviations whose meaning depends on the habit: "500 m" of running is
# meters, "30 m" of meditation is minutes. Read in the habit's default family.
AMBIGUOUS = {"m": {"distance": "meters", "time": "minutes"}}


def resolve(unit: Optional[str], habit_unit: Optional[str]) -> Optional[str]:
    """canonical(unit), with ambiguous abbreviations read in habit_unit's family.
    Stays as typed when the habit gives no hint."""
    c = canonical(unit)
    if c in AMBIGUOUS:
        family = _FAMILY_OF.get(canonical(habit_unit))
        return AMBIGUOUS[c].get(family, c)
    return c


def is_known(word: str) -> bool:
    return word.strip().lower() in ALIASES


def convert(amount: float, from_unit: str, to_unit: str) -> Optional[float]:
    """Amount in to_unit, or None if the units can't be converted."""
    src, dst = canonical(from_unit), canonical(to_unit)
    if src == dst:
        return float(amount)
    family = _FAMILY_OF.get(src)
    if family is None or family != _FAMILY_OF.get(dst):
        return None
    factors = _FAMILIES[family]
    return float(amount) * factors[src] / factors[dst]
