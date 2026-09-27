"""Date utilities for the date-change MVP chain.

Handles Chinese / ISO / short numeric date expressions, normalises them to ISO
(yyyy-mm-dd), and re-renders a new date *in the surface style of the original*
so write-backs look natural ("9 月 20 日" -> "9 月 27 日", "2026-09-20" -> "2026-09-27").
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

# ---- regex patterns, ordered longest/most-specific first ---------------------
_RE_FULL = re.compile(
    r"(?P<y>20\d{2})\s*[年/\-\.]\s*(?P<m>\d{1,2})\s*[月/\-\.]\s*(?P<d>\d{1,2})\s*日?"
)
_RE_MD = re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日")
_RE_ISO_SHORT = re.compile(r"(?<![\d])(?P<m>\d{1,2})\s*[/\-]\s*(?P<d>\d{1,2})(?![\d])")

_EN_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_EN_MONTH_NAMES = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_RE_EN_MDY = re.compile(
    r"\b(?P<mon>" + _EN_MONTH_NAMES + r")\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?(?!\d)"
    r"(?:\s*,?\s*(?P<y>20\d{2}))?",
    re.I,
)
_RE_EN_DMY = re.compile(
    r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>" + _EN_MONTH_NAMES + r")\.?"
    r"(?!\w)(?:\s*,?\s*(?P<y>20\d{2}))?",
    re.I,
)

HEDGES = ("可能", "或许", "大概", "估计", "说不定", "也许", "貌似", "好像")


def _valid(y: int, m: int, d: int) -> bool:
    return 2000 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= calendar.monthrange(y, m)[1]


def find_dates(text: str, default_year: int | None = None) -> list[dict]:
    """Find date expressions in *text*.

    Returns a list of {"start", "end", "surface", "iso"} sorted by position.
    Ambiguous mm-dd forms are included; callers decide via keyword context.
    """
    year = default_year or date.today().year
    spans: list[dict] = []
    taken: list[tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return all(e <= a or s >= b for a, b in taken)

    for match in _RE_FULL.finditer(text):
        y, m, d = int(match["y"]), int(match["m"]), int(match["d"])
        if _valid(y, m, d):
            spans.append({"start": match.start(), "end": match.end(),
                          "surface": match.group(0), "iso": f"{y:04d}-{m:02d}-{d:02d}"})
            taken.append((match.start(), match.end()))

    for match in _RE_MD.finditer(text):
        if not free(match.start(), match.end()):
            continue
        m, d = int(match["m"]), int(match["d"])
        if _valid(year, m, d):
            spans.append({"start": match.start(), "end": match.end(),
                          "surface": match.group(0), "iso": f"{year:04d}-{m:02d}-{d:02d}"})
            taken.append((match.start(), match.end()))

    for pattern in (_RE_EN_MDY, _RE_EN_DMY):
        for match in pattern.finditer(text):
            if not free(match.start(), match.end()):
                continue
            m = _EN_MONTHS[match["mon"].lower()[:3]]
            d = int(match["d"])
            y = int(match["y"]) if match["y"] else year
            if _valid(y, m, d):
                spans.append({"start": match.start(), "end": match.end(),
                              "surface": match.group(0), "iso": f"{y:04d}-{m:02d}-{d:02d}"})
                taken.append((match.start(), match.end()))

    for match in _RE_ISO_SHORT.finditer(text):
        if not free(match.start(), match.end()):
            continue
        m, d = int(match["m"]), int(match["d"])
        if _valid(year, m, d):
            spans.append({"start": match.start(), "end": match.end(),
                          "surface": match.group(0), "iso": f"{year:04d}-{m:02d}-{d:02d}"})
            taken.append((match.start(), match.end()))

    spans.sort(key=lambda s: s["start"])
    return spans


def parse_date_expr(text: str, default_year: int | None = None) -> dict | None:
    """Return the first date expression found in *text*, or None."""
    found = find_dates(text, default_year=default_year)
    return found[0] if found else None


def to_date(iso: str) -> date:
    return date.fromisoformat(iso)


def render_like(surface_old: str, new_iso: str) -> str:
    """Render *new_iso* mimicking the style/spacing of *surface_old*."""
    y, m, d = (int(p) for p in new_iso.split("-"))
    compact = surface_old.replace(" ", "").replace("\u3000", "")
    spaced = surface_old != compact  # e.g. "9 月 20 日"
    if "年" in compact:
        out = f"{y:04d}年{m}月{d}日"
    elif "月" in compact:
        out = f"{m}月{d}日"
    elif "-" in compact and len(compact.split("-")[0]) == 4:
        out = f"{y:04d}-{m:02d}-{d:02d}"
    elif "-" in compact:
        out = f"{m:02d}-{d:02d}"
    elif "/" in compact and len(compact.split("/")[0]) == 4:
        out = f"{y:04d}/{m:02d}/{d:02d}"
    elif "/" in compact:
        out = f"{m}/{d}"
    else:
        out = new_iso
    if spaced and ("年" in out or "月" in out):
        out = out.replace("年", " 年 ").replace("月", " 月 ").replace("日", " 日")
        out = re.sub(r"\s+", " ", out).strip()
    return out


def surface_variants(iso: str) -> list[str]:
    """All plausible surface spellings of *iso*, longest first (for matching)."""
    y, m, d = (int(p) for p in iso.split("-"))
    cands = [
        f"{y:04d}年{m}月{d}日",
        f"{y:04d} 年 {m} 月 {d} 日",
        f"{y:04d}-{m:02d}-{d:02d}",
        f"{y:04d}/{m:02d}/{d:02d}",
        f"{y:04d}.{m:02d}.{d:02d}",
        f"{m}月{d}日",
        f"{m} 月 {d} 日",
        f"{m:02d}-{d:02d}",
        f"{m:02d}/{d:02d}",
        f"{m}/{d}",
    ]
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out  # longest / most specific first


def days_between(iso_a: str, iso_b: str) -> int:
    """Days from a to b (positive if b is later)."""
    return (to_date(iso_b) - to_date(iso_a)).days


def shift_iso(iso: str, days: int) -> str:
    return (to_date(iso) + timedelta(days=days)).isoformat()
