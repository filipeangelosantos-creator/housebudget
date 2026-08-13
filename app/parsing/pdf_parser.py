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

import pdfplumber

# Money on a statement effectively always carries two decimal places. Requiring
# them is what stops "REF 12345" or an IBAN block being read as an amount.
MONEY_RE = re.compile(
    r"^[(+\-]?\s*[€$£R]?\$?\s*\d[\d\s.,]*[.,]\d{2}\s*[)\-]?$", re.IGNORECASE)

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


def is_money(text: str) -> bool:
    return bool(MONEY_RE.match(text.strip()))


def _match_date(tokens: list[str]) -> int:
    """How many leading tokens form one date (0 if they don't)."""
    for n in (3, 2, 1):
        if len(tokens) < n:
            continue
        joined = " ".join(tokens[:n]).strip().rstrip(",")
        if any(p.match(joined) for p in DATE_PATTERNS):
            return n
    return 0


def _has_year(text: str) -> bool:
    return bool(YEAR_RE.search(text))


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
    """Split one line into (dates, reference, description words, money words)."""
    money = [w for w in line if is_money(w["text"])]
    rest = [w for w in line if w not in money]

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
    return dates, reference, rest[idx:], money


def _document_year(pages_text: str) -> str | None:
    years = YEAR_RE.findall(pages_text)
    if not years:
        return None
    full = Counter(m.group(0) for m in YEAR_RE.finditer(pages_text))
    return full.most_common(1)[0][0]


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

    # Pass 1: find the transaction lines and where the amount columns sit.
    parsed: list = []
    money_edges: list[float] = []
    first_txn: tuple[int, float] | None = None
    for page_no, lines in enumerate(pages):
        for line in lines:
            dates, reference, desc_words, money = _split_line(line)
            if dates and money:
                parsed.append((page_no, dates, reference, desc_words, money))
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
    year = _document_year(" ".join(all_text))

    # Pass 2: build the grid, folding wrapped description lines into their row.
    rows: list[list[str]] = []
    row_index_by_line: dict[tuple[int, int], int] = {}
    for page_no, lines in enumerate(pages):
        for line_no, line in enumerate(lines):
            dates, reference, desc_words, money = _split_line(line)
            if not (dates and money):
                continue
            cells = list(dates[:n_dates]) + [""] * (n_dates - len(dates[:n_dates]))
            if year and cells and cells[0] and not _has_year(cells[0]):
                cells[0] = f"{cells[0]} {year}"
            if has_reference:
                cells.append(reference)
            description = " ".join(w["text"] for w in desc_words).strip()
            cells.append(description)
            amounts = [""] * len(columns)
            for w in money:
                amounts[_nearest(columns, w["x1"])] = w["text"].strip()
            cells.extend(amounts)
            row_index_by_line[(page_no, line_no)] = len(rows)
            rows.append(cells)

    desc_index = n_dates + (1 if has_reference else 0)
    _attach_wrapped_descriptions(pages, rows, row_index_by_line, desc_index)

    header = _build_header(pages, columns, n_dates, has_reference, first_txn)
    return ([header] + rows) if header else rows


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
            dates, _reference, desc_words, money = _split_line(line)
            if dates or money or not desc_words:
                last_row = None          # a summary or a new block, not a wrap
                continue
            extra = " ".join(w["text"] for w in desc_words).strip()
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
