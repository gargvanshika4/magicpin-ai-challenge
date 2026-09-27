"""Small, dependency-free helpers shared by the engine.

Everything here is pure and deterministic.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

NUM_RE = re.compile(r"(?<![A-Za-z_])\d[\d,]*(?:\.\d+)?")


# --------------------------------------------------------------------------- numbers

def inr(value: Any) -> str:
    """₹ with Indian digit grouping: 4999 -> ₹4,999, 125000 -> ₹1,25,000."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return f"₹{value}"
    return "₹" + indian_group(n)


def indian_group(n: int) -> str:
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + s


def num(n: Any) -> str:
    """Integer-looking numbers with grouping (2100 -> 2,100)."""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    if f.is_integer():
        return indian_group(int(f))
    return f"{f:g}"


def pct(fraction: Any, signed: bool = False) -> str:
    """0.18 -> '18%', -0.5 -> '50%' (or '-50%' when signed)."""
    try:
        f = float(fraction) * 100
    except (TypeError, ValueError):
        return str(fraction)
    val = round(abs(f), 1)
    txt = f"{val:g}%"
    if signed:
        txt = ("+" if f >= 0 else "-") + txt
    return txt


def extract_numbers(text: str) -> list[float]:
    out = []
    for m in NUM_RE.finditer(text or ""):
        raw = m.group(0).replace(",", "").rstrip(".")
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def grounding_numbers(obj: Any, acc: Optional[set] = None) -> set:
    """Collect every number that appears anywhere in a context object.

    Includes numbers embedded in strings (dates, titles, labels) and the
    percentage form of fractional values (0.18 -> 18). This is the "allowed"
    set the validator checks message numbers against.
    """
    if acc is None:
        acc = set()
    if obj is None or isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        f = float(obj)
        _add_num(acc, f)
        if -1.0 < f < 1.0 or abs(f) <= 5 and not f.is_integer():
            _add_num(acc, f * 100)
        return acc
    if isinstance(obj, str):
        for f in extract_numbers(obj):
            _add_num(acc, f)
        return acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            grounding_numbers(k, acc) if isinstance(k, str) else None
            grounding_numbers(v, acc)
        return acc
    if isinstance(obj, (list, tuple, set)):
        for v in obj:
            grounding_numbers(v, acc)
    return acc


def _add_num(acc: set, f: float) -> None:
    acc.add(round(abs(f), 1))
    acc.add(round(abs(f)))


def number_is_grounded(n: float, allowed: set) -> bool:
    return round(abs(n), 1) in allowed or (float(n).is_integer() and round(abs(n)) in allowed)


# --------------------------------------------------------------------------- dates

def parse_dt(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            dt = datetime.strptime(v[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def day_month(value: Any) -> Optional[str]:
    """'2026-12-15' -> '15 Dec'."""
    dt = parse_dt(value)
    if not dt:
        return None
    return f"{dt.day} {MONTHS[dt.month - 1]}"


def day_month_year(value: Any) -> Optional[str]:
    dt = parse_dt(value)
    if not dt:
        return None
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}"


def clock(value: Any) -> Optional[str]:
    """'2026-04-26T19:30:00+05:30' -> '7:30pm' (local time as written)."""
    dt = parse_dt(value)
    if not dt:
        return None
    h, m = dt.hour, dt.minute
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}" if m else f"{h12}{suffix}"


def weekday(value: Any) -> Optional[str]:
    dt = parse_dt(value)
    return WEEKDAYS[dt.weekday()] if dt else None


# --------------------------------------------------------------------------- text

def humanize(token: Any) -> str:
    """'6_month_cleaning' -> '6-month cleaning'; 'kids_yoga_summer_camp' -> 'kids yoga summer camp'."""
    if token is None:
        return ""
    s = str(token)
    s = re.sub(r"(\d)_(month|week|day|year)", r"\1-\2", s)
    s = re.sub(r"(\d+)(day|week|month)s?\b", r"\1-\2", s)
    # 'skin_prep_program_30-day' -> '30-day skin prep program'
    m = re.match(r"^(.*?)_(\d+-(?:day|week|month))$", s)
    if m:
        s = f"{m.group(2)}_{m.group(1)}"
    s = s.replace("_", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return re.sub(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                  lambda m: m.group(1).capitalize(), s)


_ABBR = re.compile(r"\b(Dr|Mr|Mrs|Ms|No|vs|St|p|pp|Rs|approx|e\.g|i\.e)\.(?=\s)", re.I)


def sentences(text: str) -> list[str]:
    """Sentence split that doesn't break on 'Dr.', 'p.', 'vs.' etc."""
    if not text:
        return []
    protected = _ABBR.sub(lambda m: m.group(1) + "\u2024", text.strip())
    protected = re.sub(r"(\u2024 [A-Z])\.", "\\1\u2024", protected)   # 'Dr. R. Mehta'
    parts = re.split(r"(?<=[.!?])\s+", protected)
    return [p.replace("\u2024", ".").strip() for p in parts if p.strip()]


def first_sentence(text: str) -> str:
    s = sentences(text)
    return s[0] if s else ""


def strip_dr(name: str) -> str:
    return re.sub(r"^\s*dr\.?\s+", "", name or "", flags=re.I).strip()


def join_list(items: Iterable[str], conj: str = "and") -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" {conj} " + items[-1]


def slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:maxlen].strip("_")


def clean_ws(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r" +([,.?!])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
