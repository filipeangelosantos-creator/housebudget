"""Statement file dispatch: pick the right parser by extension/content."""
from dataclasses import dataclass

from .csv_parser import Mapping, ParsedRow, apply_mapping, guess_mapping, header_signature, read_csv_rows
from .ofx_parser import looks_like_ofx, parse_ofx
from .xlsx_parser import read_xlsx_rows

SUPPORTED_EXTENSIONS = (".csv", ".txt", ".tsv", ".xlsx", ".ofx", ".qfx", ".qbo")


@dataclass
class StatementFile:
    kind: str                    # 'table' (csv/xlsx) or 'ofx'
    rows: list                   # raw cell grid for 'table', [] for 'ofx'
    parsed: list[ParsedRow]      # for 'ofx': final rows; for 'table': via mapping
    mapping: Mapping | None      # only for 'table'
    header_sig: str


def load_statement(filename: str, data: bytes,
                   mapping: Mapping | None = None) -> StatementFile:
    """Parse an uploaded statement. For tables, uses the given mapping or
    guesses one. Raises ValueError for unsupported/empty files."""
    name = (filename or "").lower()

    if name.endswith((".ofx", ".qfx", ".qbo")) or looks_like_ofx(data):
        parsed = parse_ofx(data)
        if not parsed:
            raise ValueError("No transactions found in OFX file.")
        return StatementFile(kind="ofx", rows=[], parsed=parsed,
                             mapping=None, header_sig="ofx")

    if name.endswith(".xlsx"):
        rows = read_xlsx_rows(data)
    elif name.endswith((".csv", ".txt", ".tsv")) or not name:
        rows = read_csv_rows(data)
    else:
        raise ValueError(
            "Unsupported file type. Use CSV, XLSX, OFX or QFX exports from your bank.")

    if not rows:
        raise ValueError("The file appears to be empty.")
    m = mapping or guess_mapping(rows)
    parsed = apply_mapping(rows, m)
    return StatementFile(kind="table", rows=rows, parsed=parsed,
                         mapping=m, header_sig=header_signature(rows, m))
