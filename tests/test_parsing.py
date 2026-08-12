from datetime import date

from app.parsing.csv_parser import apply_mapping, guess_mapping, read_csv_rows
from app.parsing.ofx_parser import parse_ofx
from app.parsing.statements import load_statement
from tests.conftest import SAMPLES


def test_us_checking_csv_guess():
    data = (SAMPLES / "sample-checking.csv").read_bytes()
    rows = read_csv_rows(data)
    m = guess_mapping(rows)
    assert m.header_row == 0
    assert m.date_col == 0
    assert m.desc_cols == [1]
    assert m.amount_col == 2          # Balance column excluded by header name
    assert m.dayfirst is False
    parsed = apply_mapping(rows, m)
    ok = [r for r in parsed if not r.error]
    assert len(ok) == 14
    assert ok[0].date == date(2026, 8, 1)
    assert ok[0].amount_cents == 325000
    assert "PAYROLL" in ok[0].description


def test_european_semicolon_csv():
    data = (SAMPLES / "sample-visa.csv").read_bytes()
    rows = read_csv_rows(data)
    m = guess_mapping(rows)
    assert m.date_col == 0
    assert m.amount_col == 2
    assert m.dayfirst is True         # 31-style days appear in the file
    parsed = [r for r in apply_mapping(rows, m) if not r.error]
    assert parsed[0].date == date(2026, 8, 2)
    assert parsed[0].amount_cents == 8415
    assert parsed[5].amount_cents == -85000   # payment row


def test_debit_credit_csv():
    csv_text = ("Date,Details,Withdrawals,Deposits,Balance\n"
                "2026-08-01,COFFEE SHOP,4.50,,995.50\n"
                "2026-08-02,PAYCHECK,,2000.00,2995.50\n")
    rows = read_csv_rows(csv_text.encode())
    m = guess_mapping(rows)
    assert m.debit_col == 2 and m.credit_col == 3
    parsed = [r for r in apply_mapping(rows, m) if not r.error]
    assert parsed[0].amount_cents == -450
    assert parsed[1].amount_cents == 200000


def test_headerless_csv_still_parses():
    csv_text = ("2026-08-01,GROCERY MART,-50.00\n"
                "2026-08-02,GAS STATION,-30.00\n")
    rows = read_csv_rows(csv_text.encode())
    m = guess_mapping(rows)
    parsed = [r for r in apply_mapping(rows, m) if not r.error]
    assert len(parsed) == 2
    assert parsed[0].amount_cents == -5000


def test_flip_sign():
    csv_text = "Date,Description,Amount\n2026-08-01,SHOP,25.00\n"
    rows = read_csv_rows(csv_text.encode())
    m = guess_mapping(rows)
    m.flip_sign = True
    parsed = apply_mapping(rows, m)
    assert parsed[0].amount_cents == -2500


def test_ofx():
    data = (SAMPLES / "sample-bank.ofx").read_bytes()
    parsed = parse_ofx(data)
    assert len(parsed) == 3
    assert parsed[0].date == date(2026, 7, 2)
    assert parsed[0].amount_cents == 325000
    assert parsed[0].fitid == "20260702-001"
    assert parsed[1].amount_cents == -8812
    assert "TRADER JOE" in parsed[1].description
    assert "GROCERY" in parsed[1].description      # memo appended


def test_load_statement_dispatch():
    stmt = load_statement("x.ofx", (SAMPLES / "sample-bank.ofx").read_bytes())
    assert stmt.kind == "ofx" and len(stmt.parsed) == 3
    stmt = load_statement("x.csv", (SAMPLES / "sample-checking.csv").read_bytes())
    assert stmt.kind == "table" and stmt.mapping is not None


def test_xlsx_roundtrip(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Date", "Description", "Amount"])
    ws.append([date(2026, 8, 1), "IKEA STORE", -120.5])
    ws.append([date(2026, 8, 2), "PAY", 1500])
    path = tmp_path / "s.xlsx"
    wb.save(path)
    stmt = load_statement("s.xlsx", path.read_bytes())
    ok = [r for r in stmt.parsed if not r.error]
    assert len(ok) == 2
    assert ok[0].date == date(2026, 8, 1)
    assert ok[0].amount_cents == -12050
