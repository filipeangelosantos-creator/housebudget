"""Minimal OFX/QFX parser.

OFX 1.x is SGML (tags often unclosed), OFX 2.x is XML. Both wrap each
transaction in <STMTTRN>...</STMTTRN>, so a tolerant regex scan over the
transaction blocks handles both without heavyweight dependencies.
"""
import re
from datetime import date

from .csv_parser import ParsedRow

_STMTTRN = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.IGNORECASE | re.DOTALL)


def _tag(block: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<\r\n]*)", block, re.IGNORECASE)
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


def _ofx_date(raw: str | None) -> date | None:
    if not raw:
        return None
    m = re.match(r"(\d{4})(\d{2})(\d{2})", raw)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _ofx_amount(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = raw.strip().replace(",", ".")
    try:
        return round(float(text) * 100)
    except ValueError:
        return None


def looks_like_ofx(data: bytes) -> bool:
    head = data[:2000].decode("latin-1", errors="replace").upper()
    return "<OFX>" in head or "OFXHEADER" in head or "<?OFX" in head


def parse_ofx(data: bytes) -> list[ParsedRow]:
    text = data.decode("latin-1", errors="replace")
    rows: list[ParsedRow] = []
    for m in _STMTTRN.finditer(text):
        block = m.group(1)
        d = _ofx_date(_tag(block, "DTPOSTED"))
        amount = _ofx_amount(_tag(block, "TRNAMT"))
        name = _tag(block, "NAME") or ""
        memo = _tag(block, "MEMO") or ""
        desc = name
        if memo and memo.upper() not in name.upper():
            desc = f"{name} {memo}".strip() if name else memo
        if d is None:
            rows.append(ParsedRow(raw=[block[:80]], error="no date"))
            continue
        if amount is None:
            rows.append(ParsedRow(raw=[block[:80]], error="no amount"))
            continue
        rows.append(ParsedRow(
            date=d,
            amount_cents=amount,
            description=desc or "(no description)",
            fitid=_tag(block, "FITID"),
            raw=[str(d), str(amount / 100), desc],
        ))
    return rows
