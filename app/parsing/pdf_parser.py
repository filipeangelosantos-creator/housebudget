"""Turn a text-based bank-statement PDF into the same row grid a CSV gives us.

A statement PDF has no table structure — only words with positions. The table
is reconstructed from those positions:

  * words are grouped into lines by their vertical position;
  * amounts are found with a strict money pattern (two decimal places), so
    reference numbers and IBAN digits are never mistaken for money;
  * the right edges of those amounts are clustered into columns, which is what
    keeps a debit-only row and a credit-only row in their correct columns;
  * leading date columns are consumed from the left, the rest of the line is
    the description, and a wrapped description on the next line is joined back;
  * the header line, when there is one, is emitted as row 0 so the existing
    column-mapping guesser can use names like "Saldo" or "Balance".

The result is handed to the same mapping preview as every other format, so you
still confirm the columns before anything is imported.
"""
import re
from collections import Counter
from datetime import date

import pdfplumber

from .amounts import parse_date

# Money on a statement effectively always carries two decimal places. Requiring
# them is what stops "REF 12345" or an IBAN block being read as an amount.
# Some cards mark credits with a trailing "(-)" glued to the figure: 537.76(-).
MONEY_RE = re.compile(
    r"^[(+\-]?\s*[€$£R]?\$?\s*\d[\d\s.,]*[.,]\d{2}\s*([)\-]|\(-\))?$", re.IGNORECASE)

# Date/posting markers some banks glue onto cells: 06/03/26* or 12.95†
CELL_MARKERS = "*†‡⧫§"

MONTHS = ("jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
          "fev|abr|mai|ago|set|out|dez|gen|mag|giu|lug|ott|dic|"
          "ene|abr|ago|dic|janeiro|fevereiro|marco|março|abril|maio|junho|"
          "julho|agosto|setembro|outubro|novembro|dezembro")

DATE_PATTERNS = [
    re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$"),
    re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$"),
    re.compile(r"^\d{1,2}[-/.]\d{1,2}$"),                       # no year
    re.compile(rf"^\d{{1,2}}\s*[-/ ]\s*({MONTHS})\.?\s*[-/ ]?\s*\d{{2,4}}$", re.I),
    re.compile(rf"^\d{{1,2}}\s+({MONTHS})\.?$", re.I),           # "02 Aug"
    re.compile(rf"^({MONTHS})\.?\s+\d{{1,2}}$", re.I),           # "Aug 02"
]

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
LINE_TOLERANCE = 3.0        # points; words within this share a line
COLUMN_TOLERANCE = 18.0     # points; amount right-edges within this share a column
PHRASE_GAP = 8.0            # points; a wider gap starts a new heading
WRAP_MAX_GAP = 20.0         # points; further below and it isn't a wrapped line
MAX_DATE_COLUMNS = 2
# Many statements draw text as positioned glyphs with no space characters at
# all, so word breaks have to be inferred from gaps. A gap relative to the font
# size splits "Refer to your Reward Guide" correctly where a fixed one glues it
# into "Refer toyourReward Guide".
X_TOLERANCE_RATIO = 0.15
# A long run of digits ahead of the description is the bank's reference number.
MIN_REFERENCE_DIGITS = 12


def _is_reference(text: str) -> bool:
    return text.isdigit() and len(text) >= MIN_REFERENCE_DIGITS


# --- statement sections -------------------------------------------------------
# Many statements carry the sign structurally rather than printing it: the rows
# under "Withdrawals & Debits" are money out, under "Deposits & Credits" money
# in, and a "Daily Balance" grid is not transactions at all. Headings are
# matched with spacing/punctuation removed, since these PDFs often have none.

_BALANCE_SECTIONS = ("dailybalance", "dailyendingbalance", "balancesummary",
                     "averagedailybalance", "balanceworksheet",
                     "dailybalancedetail")
_CREDIT_SECTIONS = ("depositsandcredits", "depositscredits",
                    "depositsandothercredits", "depositsandadditions",
                    "depositsotheradditions", "paymentsandcredits",
                    "paymentsandothercredits", "paymentsreceived",
                    "paymentsamount", "creditsamount", "deposits", "payments",
                    "credits", "refunds")
_DEBIT_SECTIONS = ("withdrawalsanddebits", "withdrawalsdebits",
                   "withdrawalsandothersubtractions", "otherwithdrawalsdebits",
                   "otherwithdrawals", "withdrawals",
                   "purchasesandadjustments", "purchases", "feescharged",
                   "interestcharged", "checkspaid", "electronicwithdrawals",
                   "cardpurchases", "newcharges", "fees")

# Rows that live in a transaction table but are not transactions.
_NON_TXN_DESC = ("openingbalance", "closingbalance", "beginningbalance",
                 "endingbalance", "previousbalance", "newbalance", "total",
                 "continued", "subtotal")

_NORMALIZE = re.compile(r"[^a-z]+")


def _norm(text: str) -> str:
    return _NORMALIZE.sub("", text.lower())


def _section_for(line_text: str) -> str | None:
    """'debit' / 'credit' / 'balance' when this line is a section heading."""
    n = _norm(line_text)
    if not n or len(n) > 60:
        return None
    for keys, kind in ((_BALANCE_SECTIONS, "balance"),
                       (_CREDIT_SECTIONS, "credit"),
                       (_DEBIT_SECTIONS, "debit")):
        if any(k in n for k in keys):
            return kind
    return None


def _is_non_txn_desc(desc: str) -> bool:
    n = _norm(desc)
    return any(n.startswith(k) for k in _NON_TXN_DESC)


def is_money(text: str) -> bool:
    return bool(MONEY_RE.match(text.strip().strip(CELL_MARKERS)))


def money_cell(text: str) -> str:
    """Normalize a money word for the grid: '537.76(-)' -> '-537.76'."""
    text = text.strip().strip(CELL_MARKERS).strip()
    if text.endswith("(-)"):
        text = text[:-3].strip()
        if not text.startswith("-"):
            text = "-" + text
    return text


def _clean_token(token: str) -> str:
    return token.strip().strip(CELL_MARKERS)


def _match_date(tokens: list[str]) -> int:
    """How many leading tokens form one date (0 if they don't)."""
    for n in (3, 2, 1):
        if len(tokens) < n:
            continue
        joined = " ".join(_clean_token(t) for t in tokens[:n]).strip().rstrip(",")
        if any(p.match(joined) for p in DATE_PATTERNS):
            return n
    return 0


_SHORT_YEAR_DATE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2}$")


def _has_year(text: str) -> bool:
    """'12/19/25' has a year just as much as '12/19/2025' does."""
    return bool(YEAR_RE.search(text)) or bool(_SHORT_YEAR_DATE.match(text.strip()))


def _group_lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - word["top"]) <= LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _cluster(values: list[float], tolerance: float) -> list[float]:
    """1-D clustering; returns the mean of each cluster, left to right."""
    if not values:
        return []
    clusters: list[list[float]] = [[]]
    for v in sorted(values):
        if clusters[-1] and v - clusters[-1][-1] > tolerance:
            clusters.append([])
        clusters[-1].append(v)
    return [sum(c) / len(c) for c in clusters]


def _nearest(centers: list[float], value: float) -> int:
    return min(range(len(centers)), key=lambda i: abs(centers[i] - value))


def _split_line(line: list[dict]):
    """Split a line into (dates, reference, description words, money words,
    sign markers). A standalone "(-)" is a credit marker, not description."""
    money = [w for w in line if is_money(w["text"])]
    rest = [w for w in line if w not in money]
    markers = [w for w in rest if w["text"].strip() in ("(-)", "(+)")]
    rest = [w for w in rest if w not in markers]

    dates: list[str] = []
    idx = 0
    while len(dates) < MAX_DATE_COLUMNS and idx < len(rest):
        n = _match_date([w["text"] for w in rest[idx:]])
        if not n:
            break
        dates.append(" ".join(w["text"] for w in rest[idx:idx + n]))
        idx += n

    reference = ""
    if dates and idx < len(rest) and _is_reference(rest[idx]["text"]):
        reference = rest[idx]["text"]
        idx += 1
    return dates, reference, rest[idx:], money, markers


def _document_year(pages_text: str) -> str | None:
    years = YEAR_RE.findall(pages_text)
    if not years:
        return None
    full = Counter(m.group(0) for m in YEAR_RE.finditer(pages_text))
    return full.most_common(1)[0][0]


# "Opening/Closing Date 04/12/26 - 05/11/26", "StatementPeriod05/17/26-06/16/26",
# "STATEMENT PERIOD 12/19/25 TO 01/16/26", "through March 11, 2026",
# "Statement Date: 05/11/26". Spacing is optional throughout: these PDFs often
# contain no space characters at all.
_PERIOD_RANGE = re.compile(
    r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\s*(?:-|–|—|to|through)\s*"
    r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})", re.IGNORECASE)
_PERIOD_TEXT_END = re.compile(
    r"(?:through|to|-)\s*([A-Z][a-z]{2,9}\.?\s*\d{1,2},?\s*(?:19|20)\d{2})")
_STATEMENT_DATE = re.compile(
    r"statement\s*(?:date|closing\s*date|ending)\s*:?\s*"
    r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}\s*[A-Z][a-z]{2,9}\s*(?:19|20)\d{2}|"
    r"[A-Z][a-z]{2,9}\s*\d{1,2},?\s*(?:19|20)\d{2})", re.IGNORECASE)


# The loose "through <date>" form also appears in offer terms ("Lounge access
# through September 30, 2027"), so it only counts next to a period label.
_PERIOD_LABEL = re.compile(
    r"(statement|billing|period|beginning|cycle|from)\b[^.]{0,60}$", re.IGNORECASE)


def statement_period_end(text: str) -> date | None:
    """The last day the statement covers, used to date rows that omit a year.

    Tried most reliable first: an explicit date range, then a labelled
    statement date, then prose — and prose only where a period label precedes
    it, so promotional small print cannot masquerade as the statement period.
    """
    ranges: list[date] = []
    for match in _PERIOD_RANGE.finditer(text):
        start, end = parse_date(match.group(1)), parse_date(match.group(2))
        # A real period is a forward span of at most a year or so.
        if start and end and start < end and (end - start).days <= 400:
            ranges.append(end)
    if ranges:
        return max(ranges)

    labelled = [d for d in (parse_date(m.group(1))
                            for m in _STATEMENT_DATE.finditer(text)) if d]
    if labelled:
        return max(labelled)

    prose: list[date] = []
    for match in _PERIOD_TEXT_END.finditer(text):
        before = text[max(0, match.start() - 70):match.start()].replace("\n", " ")
        if not _PERIOD_LABEL.search(before):
            continue
        parsed = parse_date(match.group(1))
        if parsed:
            prose.append(parsed)
    return max(prose) if prose else None


_MONTH_DAY = re.compile(r"^(\d{1,2})[-/.](\d{1,2})$")


def _year_for(date_text: str, period_end: date | None, fallback: str | None) -> str | None:
    """Which year a bare MM/DD belongs to.

    A statement ending 01/16/26 lists rows from both December and January;
    stamping every row with the document's most common year puts the December
    ones eleven months in the future — and makes two different statements
    collide as duplicates.
    """
    match = _MONTH_DAY.match(date_text.strip())
    if period_end is None or not match:
        return fallback
    month, day = int(match.group(1)), int(match.group(2))
    try:
        same_year = date(period_end.year, month, day)
    except ValueError:
        return str(period_end.year)
    # Rows can post a couple of days after the closing date; anything further
    # ahead belongs to the previous year.
    if (same_year - period_end).days > 5:
        return str(period_end.year - 1)
    return str(period_end.year)


def _header_line(lines: list[list[dict]], above_top: float) -> list[dict] | None:
    """The line naming the columns of the transaction table.

    It must sit above the first transaction on the same page and name both a
    date and a money column. Statements carry pages of terms and conditions
    whose prose otherwise scores as a header.
    """
    from .csv_parser import _header_role
    best, best_score = None, 0
    for line in lines:
        if line[0]["top"] >= above_top:
            break
        if any(is_money(w["text"]) for w in line):
            continue
        roles = {r for r in (_header_role(w["text"]) for w in line) if r}
        if "date" not in roles or not roles & {"amount", "debit", "credit"}:
            continue
        if len(roles) > best_score:
            best, best_score = line, len(roles)
    return best


def read_pdf_rows(data: bytes) -> list[list[str]]:
    """Extract a statement PDF as rows of cells, header row first when present.

    Raises ValueError when the file has no extractable text (a scan) or no
    rows that look like transactions.
    """
    import io

    pages: list[list[list[dict]]] = []
    all_text: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            try:
                words = page.extract_words(x_tolerance_ratio=X_TOLERANCE_RATIO)
            except TypeError:      # pdfplumber older than 0.11
                words = page.extract_words()
            pages.append(_group_lines(words))
            all_text.append(page.extract_text() or "")

    if not any(any(line for line in page) for page in pages):
        raise ValueError(
            "This PDF has no selectable text — it looks like a scan or photo. "
            "Ask your bank for a CSV, Excel or OFX export instead.")

    # Pass 1: find dated transaction lines and where the amount columns sit.
    # Section headings ("Withdrawals & Debits", "Daily Balance") are tracked as
    # we go — they carry the sign, or mark rows that are not transactions.
    parsed: list = []
    money_edges: list[float] = []
    first_txn: tuple[int, float] | None = None
    section: str | None = None
    for page_no, lines in enumerate(pages):
        for line in lines:
            dates, reference, desc_words, money, _markers = _split_line(line)
            kind = _section_for(" ".join(w["text"] for w in line))
            if kind and not dates:
                section = kind
                continue
            if not money:
                continue
            if dates:
                parsed.append((page_no, dates, reference, desc_words, money, section))
                if section != "balance":
                    money_edges.extend(w["x1"] for w in money)
                    if first_txn is None:
                        first_txn = (page_no, line[0]["top"])

    if not parsed:
        raise ValueError(
            "No transaction rows found in this PDF. If it is a statement, "
            "the layout may need a tweak — or use a CSV/Excel export instead.")

    columns = _cluster(money_edges, COLUMN_TOLERANCE)
    n_dates = min(MAX_DATE_COLUMNS, max(len(p[1]) for p in parsed))
    has_reference = any(p[2] for p in parsed)
    document_text = " ".join(all_text)
    year = _document_year(document_text)
    period_end = statement_period_end(document_text)

    # A document whose amounts carry no signs at all (no leading minus, no
    # parentheses, no (-) marker) encodes direction structurally — via its
    # sections or debit/credit columns. Only then may a section flip a sign;
    # printed signs are never overridden.
    all_money = [money_cell(w["text"]) for p in parsed for w in p[4]]
    unsigned_doc = all_money and not any(
        m.startswith("-") or m.endswith("-") for m in all_money)

    # Pass 2: build the grid, folding wrapped description lines into their row.
    rows: list[list[str]] = []
    row_index_by_line: dict[tuple[int, int], int] = {}
    section = None
    last_date_cells: list[str] | None = None
    for page_no, lines in enumerate(pages):
        for line_no, line in enumerate(lines):
            dates, reference, desc_words, money, markers = _split_line(line)
            # A heading is a heading whether or not the section's total is
            # printed beside it. Requiring a line with no money on it meant
            # "Deposits & Credits    Total Deposits & Credits   6,469.98" never
            # registered, so every deposit under it kept the sign of the
            # withdrawals section above — a whole section imported backwards.
            # A dated line is a transaction, never a heading, so a description
            # that happens to read "MOBILE DEPOSITS" cannot masquerade as one.
            kind = _section_for(" ".join(w["text"] for w in line))
            if kind and not dates:
                section = kind
                last_date_cells = None
                continue
            if not money:
                continue
            if section == "balance":
                continue
            description = " ".join(w["text"] for w in desc_words).strip()
            if _is_non_txn_desc(description):
                last_date_cells = None
                continue

            if dates:
                date_cells = [_clean_token(d) for d in dates[:n_dates]]
                date_cells += [""] * (n_dates - len(date_cells))
                for i, cell in enumerate(date_cells):
                    if cell and not _has_year(cell):
                        resolved = _year_for(cell, period_end, year)
                        if resolved:
                            date_cells[i] = f"{cell} {resolved}"
                last_date_cells = date_cells
            else:
                # Statements like HSBC's print the date once per day; the rest
                # of that day's rows inherit it. Only lines whose money sits in
                # the established columns qualify — anything else is a summary.
                if (last_date_cells is None or not description
                        or not all(_near_any(columns, w["x1"]) for w in money)):
                    continue
                date_cells = list(last_date_cells)

            cells = list(date_cells)
            if has_reference:
                cells.append(reference)
            cells.append(description)
            # A "(-)" marker beside the figure negates it — but only when no
            # section already supplies the sign, so nothing is negated twice.
            marked_negative: set[int] = set()
            if markers and unsigned_doc and section is None:
                for marker in markers:
                    if marker["text"].strip() != "(-)":
                        continue
                    left = [w for w in money if w["x1"] <= marker["x0"] + 1]
                    if left:
                        marked_negative.add(id(max(left, key=lambda w: w["x1"])))
            amounts = [""] * len(columns)
            for w in money:
                value = money_cell(w["text"])
                negate = (unsigned_doc and section == "debit") or id(w) in marked_negative
                if negate and not value.startswith("-"):
                    value = "-" + value
                amounts[_nearest(columns, w["x1"])] = value
            cells.extend(amounts)
            row_index_by_line[(page_no, line_no)] = len(rows)
            rows.append(cells)

    if not rows:
        raise ValueError(
            "No transaction rows found in this PDF. If it is a statement, "
            "the layout may need a tweak — or use a CSV/Excel export instead.")

    desc_index = n_dates + (1 if has_reference else 0)
    _attach_wrapped_descriptions(pages, rows, row_index_by_line, desc_index)

    header = _build_header(pages, columns, n_dates, has_reference, first_txn)
    return ([header] + rows) if header else rows


def _near_any(centers: list[float], value: float) -> bool:
    return bool(centers) and min(abs(c - value) for c in centers) <= COLUMN_TOLERANCE


def _attach_wrapped_descriptions(pages, rows, row_index_by_line, desc_index) -> None:
    """Join a description that continued onto the following line.

    Only the line immediately below a transaction, and only when it sits close
    enough to be part of the same row — otherwise a page footer far down the
    page would be swallowed into the last transaction.
    """
    for page_no, lines in enumerate(pages):
        last_row: int | None = None
        last_top: float = 0.0
        for line_no, line in enumerate(lines):
            key = (page_no, line_no)
            if key in row_index_by_line:
                last_row = row_index_by_line[key]
                last_top = line[0]["top"]
                continue
            if last_row is None:
                continue
            if line[0]["top"] - last_top > WRAP_MAX_GAP:
                last_row = None          # too far below to belong to that row
                continue
            dates, _reference, desc_words, money, _markers = _split_line(line)
            if dates or money or not desc_words:
                last_row = None          # a summary or a new block, not a wrap
                continue
            extra = " ".join(w["text"] for w in desc_words).strip()
            # A section heading or totals line below a transaction is a new
            # block, never a continuation of the description above it.
            if _section_for(extra) or _is_non_txn_desc(extra):
                last_row = None
                continue
            if extra:
                rows[last_row][desc_index] = (
                    rows[last_row][desc_index] + " " + extra).strip()
            last_row = None              # only ever fold in one extra line


def _phrases(line: list[dict]) -> list[dict]:
    """Group a line's words into headings: "Paid out" and "Data Mov." are one
    heading each, not two words."""
    groups: list[list[dict]] = []
    for word in sorted(line, key=lambda w: w["x0"]):
        if groups and word["x0"] - groups[-1][-1]["x1"] <= PHRASE_GAP:
            groups[-1].append(word)
        else:
            groups.append([word])
    return [{"text": " ".join(w["text"] for w in g),
             "x0": g[0]["x0"], "x1": g[-1]["x1"]} for g in groups]


def _build_header(pages, columns: list[float], n_dates: int, has_reference: bool,
                  first_txn: tuple[int, float] | None) -> list[str] | None:
    """Emit the statement's own column names in the same slots as the data."""
    if first_txn is None:
        return None
    page_no, txn_top = first_txn
    line = _header_line(pages[page_no], txn_top)
    if not line:
        return None

    headings = _phrases(line)
    used: set[int] = set()
    amount_names = [""] * len(columns)
    # Amount headings are right-aligned over their column, like the figures.
    for i, center in enumerate(columns):
        best, best_dist = None, COLUMN_TOLERANCE * 2.5
        for j, h in enumerate(headings):
            if j in used:
                continue
            dist = min(abs(h["x1"] - center), abs((h["x0"] + h["x1"]) / 2 - center))
            if dist < best_dist:
                best, best_dist = j, dist
        if best is not None:
            used.add(best)
            amount_names[i] = headings[best]["text"]

    leading = [h["text"] for j, h in enumerate(headings) if j not in used]
    dates = (leading[:n_dates] + [""] * max(0, n_dates - len(leading)))[:n_dates]
    remaining = leading[n_dates:]
    reference = [remaining.pop(0) if remaining else "Reference"] if has_reference else []
    description = " ".join(remaining).strip() or "Description"
    header = dates + reference + [description] + amount_names
    return header if any(h.strip() for h in header) else None


def looks_like_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"
