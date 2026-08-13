"""Tolerant parsing of money amounts and dates as found in bank exports."""
import re
from datetime import date, datetime

from dateutil import parser as dateparser

_CURRENCY_JUNK = re.compile(r"[^\d,.\-+()]")
_DR_CR = re.compile(r"\b(DR|DB|CR)\b\.?$", re.IGNORECASE)


def parse_amount(raw) -> int | None:
    """Parse a money string into integer cents. Returns None if not a number.

    Handles: $1,234.56 / 1.234,56 / (123.45) / 123.45- / 12,50 / 1 234,56 /
    trailing DR/CR markers. Sign convention of the source is preserved.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return round(float(raw) * 100)
    text = str(raw).strip()
    if not text:
        return None

    sign = 1
    m = _DR_CR.search(text)
    if m:
        if m.group(1).upper() in ("DR", "DB"):
            sign = -1
        text = text[: m.start()].strip()
    text = text.replace("\xa0", " ").replace(" ", "")
    if text.startswith("(") and text.endswith(")"):
        sign = -sign
        text = text[1:-1]
    if text.endswith("-"):
        sign = -sign
        text = text[:-1]
    if text.startswith("-"):
        sign = -sign
        text = text[1:]
    elif text.startswith("+"):
        text = text[1:]
    text = _CURRENCY_JUNK.sub("", text)
    if not text or not re.search(r"\d", text):
        return None

    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        # Rightmost separator is the decimal separator.
        if text.rfind(".") > text.rfind(","):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif has_comma:
        # "12,50" -> decimal; "1,234" (exactly 3 digits after, valid grouping) -> thousands
        intpart, _, frac = text.rpartition(",")
        if len(frac) == 3 and text.count(",") >= 1 and _valid_grouping(text, ","):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".", 1) if text.count(",") == 1 else None
            if text is None:
                return None
    elif has_dot:
        # "1.234" / "1.234.567" with valid 3-digit groups is a thousands
        # separator (European style) — money never has 3 decimal places.
        _, _, frac = text.rpartition(".")
        if len(frac) == 3 and _valid_grouping(text, "."):
            text = text.replace(".", "")
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return sign * round(value * 100)


def _valid_grouping(text: str, sep: str) -> bool:
    parts = text.split(sep)
    if len(parts[0]) == 0 or len(parts[0]) > 3:
        return False
    return all(len(p) == 3 for p in parts[1:])


_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_YMD_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def parse_date(raw, dayfirst: bool = False) -> date | None:
    """Parse a date value from a statement cell. Returns None on failure."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip().strip('"')
    if not text:
        return None
    m = _ISO_DATE.match(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _YMD_COMPACT.match(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    try:
        return dateparser.parse(text, dayfirst=dayfirst, yearfirst=False).date()
    except (ValueError, OverflowError):
        return None


def dayfirst_by_month_spread(values: list) -> bool | None:
    """Disambiguate 01/08/2026 when no day above 12 settles it.

    A statement covers roughly one month, so the reading that keeps the dates
    inside a month or two is the right one: read month-first, 01/08 02/08 03/08
    becomes January, February, March.
    """
    def spread(dayfirst: bool) -> tuple[int, int]:
        months = set()
        valid = 0
        for v in values:
            d = parse_date(v, dayfirst=dayfirst)
            if d is not None:
                months.add((d.year, d.month))
                valid += 1
        return len(months), valid

    day_months, day_valid = spread(True)
    month_months, month_valid = spread(False)
    if min(day_valid, month_valid) < 3:
        return None
    if day_months < month_months:
        return True
    if month_months < day_months:
        return False
    return None


def infer_dayfirst(values: list) -> bool | None:
    """Look at raw date strings like 13/05/2026: if any first component > 12,
    dates are day-first; if any second component > 12, month-first.
    Returns None when ambiguous."""
    first_gt12 = second_gt12 = False
    pat = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})")
    for v in values:
        if v is None:
            continue
        m = pat.match(str(v).strip())
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 and b <= 12:
            first_gt12 = True
        if b > 12 and a <= 12:
            second_gt12 = True
    if first_gt12 and not second_gt12:
        return True
    if second_gt12 and not first_gt12:
        return False
    return None
