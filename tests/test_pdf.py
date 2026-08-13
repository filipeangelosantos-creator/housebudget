from datetime import date

import pytest

from app.parsing.pdf_parser import is_money, looks_like_pdf, read_pdf_rows
from app.parsing.statements import load_statement
from tests import pdf_fixtures as F


def parsed(fixture_bytes):
    st = load_statement("statement.pdf", fixture_bytes)
    return st, [r for r in st.parsed if not r.error]


def test_money_pattern_ignores_reference_numbers():
    for good in ("84,15", "1.234,56", "-1,450.00", "3,250.00", "€45,00", "(123.45)"):
        assert is_money(good), good
    # IBAN blocks, card fragments, store numbers and refs must not read as money
    for bad in ("0000", "PT50", "#3181", "88213", "1121", "****4421", "2026", "1/1"):
        assert not is_money(bad), bad


def test_looks_like_pdf():
    assert looks_like_pdf(F.no_year_in_rows())
    assert not looks_like_pdf(b"Date,Description,Amount\n")


def test_amount_and_balance_layout():
    st, rows = parsed(F.us_style_amount_and_balance())
    assert len(rows) == 7
    # the balance column must not be mistaken for the amount
    assert st.mapping.amount_col is not None
    assert rows[0].date == date(2026, 8, 1)
    assert rows[0].amount_cents == 325000
    assert rows[0].description == "PAYROLL ACME CORP DIRECT DEP"
    assert rows[2].amount_cents == -14237
    assert rows[-1].amount_cents == -23180
    # nothing from the page furniture leaked in as a transaction
    assert all("Closing balance" not in r.description for r in rows)
    assert all("Page 1" not in r.description for r in rows)


def test_debit_credit_layout_with_european_numbers():
    st, rows = parsed(F.pt_style_debit_credit())
    assert st.mapping.debit_col is not None and st.mapping.credit_col is not None
    assert len(rows) == 6
    assert rows[0].date == date(2026, 8, 2)
    assert rows[0].amount_cents == -8415          # debit column -> money out
    assert rows[2].amount_cents == 150000         # credit column -> money in
    assert rows[-1].amount_cents == -2490


def test_wrapped_description_is_joined_but_footer_is_not():
    _, rows = parsed(F.pt_style_debit_credit())
    wrapped = rows[2].description
    assert "TRANSFERENCIA SEPA RECEBIDA" in wrapped
    assert "ORDENANTE MARIA SILVA" in wrapped     # continuation line folded in
    assert all("Pagina" not in r.description for r in rows)   # footer was not


def test_year_taken_from_statement_header_when_rows_omit_it():
    _, rows = parsed(F.no_year_in_rows())
    assert len(rows) == 4
    assert rows[0].date == date(2026, 8, 2)
    assert rows[-1].date == date(2026, 8, 21)


def test_ambiguous_dates_resolved_by_keeping_the_statement_in_one_month():
    """01/08 02/08 03/08 is a day-first August, not January to March."""
    st, rows = parsed(F.us_style_amount_and_balance())
    assert st.mapping.dayfirst is True
    assert {r.date.month for r in rows} == {8}


def test_multi_page_statement_keeps_every_row():
    st, rows = parsed(F.two_pages())
    assert len(rows) == 6                          # 3 on each page
    assert rows[0].amount_cents == 240000          # "Paid in" column
    assert rows[1].amount_cents == -6420           # "Paid out" column
    assert rows[4].amount_cents == 2999            # refund, paid in
    assert all("Page" not in r.description for r in rows)
    assert all("BIG BANK" not in r.description for r in rows)


def test_card_statement_with_reference_numbers():
    """Regression for the first real import, which produced dates from the
    wrong column, descriptions containing the amount, and amounts in the
    hundreds of septillions."""
    st, rows = parsed(F.card_with_reference_numbers_and_no_spaces())
    assert len(rows) == 5

    # the reference number is its own column, so it stays out of both the
    # description and the amount
    assert st.mapping.amount_col is not None
    header = st.rows[0]
    assert "Reference" in " ".join(header)
    for row in rows:
        assert "82305096" not in row.description
        assert abs(row.amount_cents) < 10_000_00       # no absurd magnitudes

    assert rows[0].date == date(2026, 5, 30)           # transaction date, not post
    assert rows[0].amount_cents == -2018
    assert rows[0].description.startswith("AMAZON MARK")
    assert rows[2].amount_cents == 8415
    assert rows[-1].amount_cents == 22896

    # words are recovered even though the PDF contains no space characters
    assert "STOP & SHOP" in rows[2].description
    assert "INTEREST CHARGE" in rows[-1].description


def test_terms_and_conditions_page_is_not_mistaken_for_the_table_header():
    rows = read_pdf_rows(F.card_with_reference_numbers_and_no_spaces())
    header = rows[0]
    assert "Account Information" not in " ".join(header)
    assert "Date" in " ".join(header) and "Amount" in " ".join(header)


def test_scanned_pdf_gives_an_actionable_message():
    with pytest.raises(ValueError, match="scan or photo"):
        load_statement("scan.pdf", F.scanned_like_no_text())


def test_pdf_without_transactions_is_rejected_clearly():
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(60, 700, "Dear customer, thank you for banking with us.")
    c.save()
    with pytest.raises(ValueError, match="No transaction rows"):
        load_statement("letter.pdf", buf.getvalue())


def test_pdf_detected_without_the_extension():
    st = load_statement("statement", F.no_year_in_rows())
    assert st.kind == "table"
    assert len([r for r in st.parsed if not r.error]) == 4


def test_pdf_import_dedupes_like_any_other_statement(conn):
    from app.services import importer
    conn.execute("INSERT INTO accounts (id, name, type, created_at) "
                 "VALUES (1, 'Checking', 'checking', 'now')")
    conn.execute("INSERT INTO users (id, username, display_name, password_hash, "
                 "created_at) VALUES (1, 'u', 'u', 'x', 'now')")
    conn.commit()
    data = F.us_style_amount_and_balance()

    first = importer.commit_import(conn, 1, "s.pdf", data,
                                   load_statement("s.pdf", data).parsed, 1)
    assert first.added == 7
    again = importer.commit_import(conn, 1, "s.pdf", data,
                                   load_statement("s.pdf", data).parsed, 1)
    assert again.added == 0 and again.duplicates == 7
