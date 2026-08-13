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


def test_columnar_bank_with_credit_column_first():
    """HSBC shape: ADDITIONS (money in) left of SUBTRACTIONS. The column
    headings must set the signs — assuming first-money-column-is-debit
    inverted every transaction on this layout."""
    st, rows = parsed(F.columnar_additions_subtractions())
    assert st.mapping.credit_col is not None and st.mapping.debit_col is not None
    assert st.mapping.credit_col < st.mapping.debit_col
    amounts = [r.amount_cents for r in rows]
    assert amounts == [320704, -25000, -134704, 15000]
    assert sum(amounts) == 176000


def test_columnar_bank_dates_inherited_within_a_day():
    """The date is printed once per day; later rows that day carry none and
    must inherit it rather than be dropped."""
    _, rows = parsed(F.columnar_additions_subtractions())
    assert len(rows) == 4                                  # none silently lost
    assert rows[0].date == rows[1].date == date(2025, 12, 22)
    assert rows[2].date == rows[3].date == date(2025, 12, 23)


def test_opening_and_ending_balance_rows_are_not_transactions():
    _, rows = parsed(F.columnar_additions_subtractions())
    descs = " ".join(r.description for r in rows)
    assert "OPENING" not in descs and "ENDING" not in descs


def test_two_digit_year_dates_do_not_get_another_year_appended():
    _, rows = parsed(F.columnar_additions_subtractions())
    assert rows[0].date == date(2025, 12, 22)              # not 2026, not mangled


def test_sectioned_checking_signs_come_from_headings():
    st, rows = parsed(F.sectioned_checking())
    by_desc = {r.description.split()[0]: r.amount_cents for r in rows}
    assert by_desc["EVERSOURCE"] == -11486                 # withdrawals section
    assert by_desc["VENMO"] == -10000
    assert by_desc["TRANSFER"] == 25000                    # deposits section
    assert sum(r.amount_cents for r in rows) == 3514


def test_daily_balance_grid_is_not_transactions():
    _, rows = parsed(F.sectioned_checking())
    assert len(rows) == 3                                  # not 3 + balance pairs
    amounts = {r.amount_cents for r in rows}
    assert 126210 not in amounts and -126210 not in amounts


def test_credit_marker_with_sections_strips_marker_and_uses_section_sign():
    st, rows = parsed(F.card_with_credit_markers(with_sections=True))
    assert all("(-)" not in r.description for r in rows)
    by_desc = {r.description: r.amount_cents for r in rows}
    assert by_desc["PAYMENT RECEIVED"] == 53776            # credits section
    assert by_desc["TJMAXX #0569 BROOKLINE MA"] == 1061    # a return: money in
    assert by_desc["TJMAXX #0098 SUNRISE FL"] == -18611    # purchases section
    assert by_desc["STAR MARKET AUBURNDALE MA"] == -4500


def test_credit_marker_without_sections_negates_its_row():
    st, rows = parsed(F.card_with_credit_markers(with_sections=False))
    by_desc = {r.description: r.amount_cents for r in rows}
    assert by_desc["PAYMENT RECEIVED"] == -53776           # marker negates
    assert by_desc["TJMAXX #0569 BROOKLINE MA"] == -1061
    assert by_desc["TJMAXX #0098 SUNRISE FL"] == 18611     # unmarked untouched
    assert all("(-)" not in d for d in by_desc)


def test_posting_asterisk_on_date_still_recognised():
    """A date like 06/03/26* must not stop the payment row being captured —
    on a real statement this silently dropped the whole payment."""
    _, rows = parsed(F.dated_with_posting_asterisk())
    assert len(rows) == 3
    assert rows[0].date == date(2026, 6, 3)
    assert rows[0].amount_cents == -53722


def test_printed_signs_are_never_overridden_by_sections():
    """A signed document (intrinsic minuses) keeps its printed signs even when
    section headings are present."""
    _, rows = parsed(F.dated_with_posting_asterisk())
    by = {r.description[:8]: r.amount_cents for r in rows}
    assert by["AUTOPAY "] == -53722                        # stays negative
    assert by["SOME STO"] == 4500                          # stays positive


def test_year_comes_from_the_statement_period_not_the_footer():
    """Rows are MM/DD only. The page's one 4-digit year is a year-to-date
    footer, which for a statement spanning New Year is the wrong year for
    half the rows."""
    pdf = F.card_month_day_only(
        "12/12/25 - 01/11/26",
        [("12/15", "GAS STATION 42", "40.00"),
         ("12/28", "GROCERY RUN", "80.00"),
         ("01/04", "COFFEE SHOP", "5.00")],
        totals_year="2026")
    _, rows = parsed(pdf)
    assert [r.date for r in rows] == [
        date(2025, 12, 15), date(2025, 12, 28), date(2026, 1, 4)]


def test_two_consecutive_statements_do_not_collide(conn):
    """The reported bug: a later statement reported every row as already
    imported. Stamping both months with one year made them identical."""
    from app.services import importer
    conn.execute("INSERT INTO accounts (id, name, type, created_at) "
                 "VALUES (1, 'Chase CC', 'credit', 'now')")
    conn.execute("INSERT INTO users (id, username, display_name, password_hash, "
                 "created_at) VALUES (1, 'u', 'u', 'x', 'now')")
    conn.commit()

    # Same merchants and amounts each month — a standing subscription pattern.
    lines = [("11", "NETFLIX.COM", "15.99"), ("14", "STOP & SHOP 0049", "32.55"),
             ("21", "BURGER KING #5100", "21.80")]
    jan = F.card_month_day_only(
        "12/12/25 - 01/11/26", [(f"12/{d}", m, a) for d, m, a in lines])
    feb = F.card_month_day_only(
        "01/12/26 - 02/11/26", [(f"01/{d}", m, a) for d, m, a in lines])

    first = importer.commit_import(conn, 1, "Statements-1.pdf", jan,
                                   load_statement("a.pdf", jan).parsed, 1)
    second = importer.commit_import(conn, 1, "Statements-2.pdf", feb,
                                    load_statement("b.pdf", feb).parsed, 1)
    assert first.added == 3
    assert second.added == 3 and second.duplicates == 0

    months = {r["m"] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,7) AS m FROM transactions").fetchall()}
    assert months == {"2025-12", "2026-01"}


def test_preview_says_where_duplicates_came_from(conn):
    """'24 already imported' has to be checkable, or a parsing fault is
    indistinguishable from a genuinely repeated statement."""
    from app.services import importer
    conn.execute("INSERT INTO accounts (id, name, type, created_at) "
                 "VALUES (1, 'Chase CC', 'credit', 'now')")
    conn.execute("INSERT INTO users (id, username, display_name, password_hash, "
                 "created_at) VALUES (1, 'u', 'u', 'x', 'now')")
    conn.commit()
    pdf = F.card_month_day_only("01/12/26 - 02/11/26",
                                [("01/14", "STOP & SHOP 0049", "32.55")])
    importer.commit_import(conn, 1, "Statements-3.pdf", pdf,
                           load_statement("a.pdf", pdf).parsed, 1)

    stats = importer.preview_stats(conn, 1, load_statement("a.pdf", pdf).parsed)
    assert stats.duplicates == 1
    assert len(stats.duplicate_sources) == 1
    source = stats.duplicate_sources[0]
    assert source["filename"] == "Statements-3.pdf"
    assert source["count"] == 1
    assert source["date_min"] == "2026-01-14"


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
