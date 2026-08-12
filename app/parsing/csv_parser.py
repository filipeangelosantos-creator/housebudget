"""CSV/TSV statement parsing with header + column auto-detection.

Banks export wildly different CSVs (delimiters, encodings, languages, single
signed amount vs. separate debit/credit columns, day-first dates, preamble
lines before the table). This module guesses a column mapping, which the user
confirms on the import preview screen; confirmed mappings are saved per
account+header signature so future imports are one click.
"""
import csv
import io
import re
from dataclasses import dataclass, field, asdict

from .amounts import parse_amount, parse_date, infer_dayfirst

DELIMITERS = [",", ";", "\t", "|"]

HEADER_KEYWORDS = {
    "date": ["date", "data", "fecha", "posted", "post date", "transaction date",
             "booking date", "value date", "data mov", "data valor", "processed",
             "data lanc", "data lançamento", "dt", "datum"],
    "desc": ["description", "descricao", "descrição", "memo", "payee",
             "narrative", "details", "detail", "transaction", "merchant", "name",
             "concepto", "libelle", "libellé", "historico", "histórico",
             "movimento", "reference", "referencia"],
    "amount": ["amount", "montante", "valor", "importe", "montant", "value",
               "transaction amount", "amt", "betrag"],
    "debit": ["debit", "debito", "débito", "withdrawal", "withdrawals",
              "money out", "paid out", "charge", "cargo", "saida", "saída",
              "debe", "out"],
    "credit": ["credit", "credito", "crédito", "deposit", "deposits",
               "money in", "paid in", "abono", "entrada", "haber", "in"],
    "balance": ["balance", "saldo", "running balance", "solde",
                "saldo contabilistico", "saldo disponivel"],
}


@dataclass
class Mapping:
    date_col: int = 0
    desc_cols: list[int] = field(default_factory=list)
    amount_col: int | None = None
    debit_col: int | None = None
    credit_col: int | None = None
    flip_sign: bool = False
    dayfirst: bool = False
    header_row: int | None = None  # index of header row; data starts after it

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Mapping":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class ParsedRow:
    date: object = None          # datetime.date
    amount_cents: int | None = None
    description: str = ""
    fitid: str | None = None
    raw: list = field(default_factory=list)
    error: str | None = None


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="".join(DELIMITERS))
        return dialect.delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in DELIMITERS}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","


def read_csv_rows(data: bytes) -> list[list[str]]:
    text = decode_bytes(data)
    delim = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    return [[c.strip() for c in row] for row in reader if any(c.strip() for c in row)]


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", " ", str(cell).strip().lower().strip('"'))


def _header_role(cell: str) -> str | None:
    h = _norm_header(cell)
    if not h:
        return None
    for role in ("balance", "debit", "credit", "amount", "date", "desc"):
        for kw in HEADER_KEYWORDS[role]:
            if h == kw or h.startswith(kw + " ") or (len(kw) > 3 and kw in h):
                return role
    return None


def find_header_row(rows: list[list]) -> int | None:
    for i, row in enumerate(rows[:10]):
        roles = [r for r in (_header_role(c) for c in row) if r]
        if len(roles) >= 2 and "date" in roles:
            return i
    return None


_DATEISH = re.compile(r"(\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4})|(^\d{8}$)|"
                      r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                      re.IGNORECASE)


def _looks_like_date(cell) -> bool:
    if cell is None or cell == "":
        return False
    if not isinstance(cell, str):
        return hasattr(cell, "year")  # datetime/date from xlsx
    return bool(_DATEISH.search(cell)) and parse_date(cell) is not None


def guess_mapping(rows: list[list]) -> Mapping:
    header_row = find_header_row(rows)
    data = rows[header_row + 1:] if header_row is not None else rows
    sample = [r for r in data if sum(1 for c in r if c not in (None, "")) >= 2][:60]
    width = max((len(r) for r in sample), default=0)

    header = rows[header_row] if header_row is not None else []
    roles = {i: _header_role(header[i]) if i < len(header) else None for i in range(width)}

    date_scores, amount_scores, text_lens = {}, {}, {}
    for col in range(width):
        cells = [r[col] if col < len(r) else None for r in sample]
        filled = [c for c in cells if c not in (None, "")]
        if not filled:
            continue
        date_scores[col] = sum(1 for c in filled if _looks_like_date(c)) / len(filled)
        amount_scores[col] = sum(
            1 for c in filled if not _looks_like_date(c) and parse_amount(c) is not None
        ) / len(filled)
        texts = [str(c) for c in filled
                 if not _looks_like_date(c) and parse_amount(c) is None]
        text_lens[col] = (sum(len(t) for t in texts) / len(texts)) if texts else 0.0

    m = Mapping(header_row=header_row)

    # --- date column
    hinted = [i for i, r in roles.items() if r == "date"]
    date_candidates = [i for i in hinted if date_scores.get(i, 0) >= 0.5] or \
        sorted([i for i, s in date_scores.items() if s >= 0.6],
               key=lambda i: (-date_scores[i], i))
    m.date_col = date_candidates[0] if date_candidates else 0

    # --- amount column(s)
    balance_cols = {i for i, r in roles.items() if r == "balance"}
    amount_candidates = [i for i, s in amount_scores.items()
                         if s >= 0.6 and i != m.date_col and i not in balance_cols]
    debit_hint = [i for i, r in roles.items() if r == "debit" and i in amount_candidates]
    credit_hint = [i for i, r in roles.items() if r == "credit" and i in amount_candidates]
    amount_hint = [i for i, r in roles.items() if r == "amount" and i in amount_candidates]
    if debit_hint and credit_hint:
        m.debit_col, m.credit_col = debit_hint[0], credit_hint[0]
    elif amount_hint:
        m.amount_col = amount_hint[0]
    elif len(amount_candidates) >= 2 and _complementary(sample, amount_candidates[:2]):
        m.debit_col, m.credit_col = amount_candidates[0], amount_candidates[1]
    elif amount_candidates:
        m.amount_col = amount_candidates[0]

    # --- description column(s): longest text columns
    used = {m.date_col, m.amount_col, m.debit_col, m.credit_col} | balance_cols
    desc_hinted = [i for i, r in roles.items() if r == "desc" and i not in used]
    if desc_hinted:
        m.desc_cols = desc_hinted[:2]
    else:
        by_len = sorted((i for i in text_lens if i not in used and text_lens[i] >= 3),
                        key=lambda i: -text_lens[i])
        m.desc_cols = sorted(by_len[:2])
    if not m.desc_cols:
        fallback = [i for i in range(width) if i not in used]
        m.desc_cols = fallback[:1]

    # --- day-first dates?
    raw_dates = [r[m.date_col] if m.date_col < len(r) else None for r in sample]
    inferred = infer_dayfirst([c for c in raw_dates if isinstance(c, str)])
    if inferred is not None:
        m.dayfirst = inferred
    return m


def _complementary(sample: list[list], pair: list[int]) -> bool:
    """True when two columns look like debit/credit: rows fill one or the other."""
    a, b = pair
    single = both = 0
    for r in sample:
        va = r[a] if a < len(r) else None
        vb = r[b] if b < len(r) else None
        fa, fb = va not in (None, ""), vb not in (None, "")
        if fa and fb:
            both += 1
        elif fa or fb:
            single += 1
    total = single + both
    return total > 0 and single / total >= 0.8


def apply_mapping(rows: list[list], mapping: Mapping) -> list[ParsedRow]:
    start = mapping.header_row + 1 if mapping.header_row is not None else 0
    out: list[ParsedRow] = []
    for row in rows[start:]:
        if sum(1 for c in row if c not in (None, "")) < 2:
            continue
        cell = lambda i: (row[i] if i is not None and i < len(row) else None)
        d = parse_date(cell(mapping.date_col), dayfirst=mapping.dayfirst)
        if d is None:
            out.append(ParsedRow(raw=row, error="no date"))
            continue

        if mapping.amount_col is not None:
            amount = parse_amount(cell(mapping.amount_col))
        else:
            debit = parse_amount(cell(mapping.debit_col))
            credit = parse_amount(cell(mapping.credit_col))
            if debit is None and credit is None:
                amount = None
            else:
                amount = (abs(credit) if credit else 0) - (abs(debit) if debit else 0)
        if amount is None:
            out.append(ParsedRow(raw=row, error="no amount"))
            continue
        if mapping.flip_sign:
            amount = -amount

        parts = [str(cell(i)).strip() for i in mapping.desc_cols
                 if cell(i) not in (None, "")]
        desc = " · ".join(p for p in parts if p) or "(no description)"
        out.append(ParsedRow(date=d, amount_cents=amount, description=desc, raw=row))
    return out


def header_signature(rows: list[list], mapping: Mapping) -> str:
    if mapping.header_row is not None and mapping.header_row < len(rows):
        cells = [_norm_header(c) for c in rows[mapping.header_row]]
        return "|".join(cells)
    width = max((len(r) for r in rows), default=0)
    return f"ncols:{width}"
