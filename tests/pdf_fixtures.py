"""Bank-statement PDFs built in memory, used to test the PDF importer.

Real statements can't go in the repo, so these reproduce the layouts that
matter: a single signed amount beside a running balance, separate debit and
credit columns, descriptions that wrap onto a second line, page furniture
around the table, and rows whose dates omit the year.
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4


def _draw_right(c, x, y, text):
    c.drawRightString(x, y, text)


def us_style_amount_and_balance() -> bytes:
    """Date | Description | Amount (signed) | Balance."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, H - 60, "FIRST NATIONAL BANK")
    c.setFont("Helvetica", 9)
    c.drawString(50, H - 75, "Statement period 01 Aug 2026 - 31 Aug 2026")
    c.drawString(50, H - 88, "Account 1234567890   Sort 00-11-22")

    y = H - 120
    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, y, "Date")
    c.drawString(120, y, "Description")
    _draw_right(c, 430, y, "Amount")
    _draw_right(c, 520, y, "Balance")
    c.line(50, y - 4, 520, y - 4)

    rows = [
        ("01/08/2026", "PAYROLL ACME CORP DIRECT DEP", "3,250.00", "5,410.22"),
        ("01/08/2026", "MORTGAGE PAYMENT HOMELOAN CO", "-1,450.00", "3,960.22"),
        ("02/08/2026", "WALMART SUPERCENTER #3181", "-142.37", "3,817.85"),
        ("03/08/2026", "STARBUCKS #55231 MAIN ST", "-6.45", "3,811.40"),
        ("05/08/2026", "TRANSFER TO VISA ****4421", "-850.00", "2,961.40"),
        ("09/08/2026", "ATM WITHDRAWAL BRANCH 042", "-100.00", "2,861.40"),
        ("11/08/2026", "COSTCO WHOLESALE #692", "-231.80", "2,629.60"),
    ]
    c.setFont("Helvetica", 9)
    y -= 20
    for date, desc, amount, balance in rows:
        c.drawString(50, y, date)
        c.drawString(120, y, desc)
        _draw_right(c, 430, y, amount)
        _draw_right(c, 520, y, balance)
        y -= 16

    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, y - 10, "Closing balance")
    _draw_right(c, 520, y - 10, "2,629.60")
    c.setFont("Helvetica", 7)
    c.drawString(50, 40, "Page 1 of 1 - This is not a tax document")
    c.save()
    return buf.getvalue()


def pt_style_debit_credit() -> bytes:
    """Portuguese layout: two date columns, Débito / Crédito / Saldo,
    comma decimals, dot thousands, and a description wrapping to a 2nd line."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(40, H - 55, "BANCO EXEMPLO")
    c.setFont("Helvetica", 8)
    c.drawString(40, H - 70, "Extrato de conta - Agosto 2026")
    c.drawString(40, H - 82, "IBAN PT50 0000 0000 0000 0000 0000 0")

    y = H - 115
    c.setFont("Helvetica-Bold", 8)
    c.drawString(40, y, "Data Mov.")
    c.drawString(100, y, "Data Valor")
    c.drawString(165, y, "Descricao")
    _draw_right(c, 400, y, "Debito")
    _draw_right(c, 465, y, "Credito")
    _draw_right(c, 545, y, "Saldo")
    c.line(40, y - 4, 545, y - 4)

    rows = [
        ("02-08-2026", "02-08-2026", "COMPRA CONTINENTE MATOSINHOS", None,
         "84,15", None, "1.234,56"),
        ("03-08-2026", "03-08-2026", "GALP COMBUSTIVEIS PORTO", None,
         "45,00", None, "1.189,56"),
        ("05-08-2026", "05-08-2026", "TRANSFERENCIA SEPA RECEBIDA",
         "ORDENANTE MARIA SILVA REF 88213", None, "1.500,00", "2.689,56"),
        ("09-08-2026", "09-08-2026", "PAGAMENTO SERVICOS EDP COMERCIAL", None,
         "96,50", None, "2.593,06"),
        ("15-08-2026", "15-08-2026", "LEVANTAMENTO ATM AV LIBERDADE", None,
         "100,00", None, "2.493,06"),
        ("28-08-2026", "28-08-2026", "VODAFONE PT COMUNICACOES", None,
         "24,90", None, "2.468,16"),
    ]
    y -= 18
    for date1, date2, desc, cont, debit, credit, saldo in rows:
        c.setFont("Helvetica", 8)
        c.drawString(40, y, date1)
        c.drawString(100, y, date2)
        c.drawString(165, y, desc)
        if debit:
            _draw_right(c, 400, y, debit)
        if credit:
            _draw_right(c, 465, y, credit)
        _draw_right(c, 545, y, saldo)
        y -= 12
        if cont:                      # wrapped description, no date, no amount
            c.setFont("Helvetica", 7)
            c.drawString(165, y, cont)
            y -= 12

    c.setFont("Helvetica", 7)
    c.drawString(40, 45, "Pagina 1/1")
    c.save()
    return buf.getvalue()


def no_year_in_rows() -> bytes:
    """Rows show only day/month; the year lives in the statement header."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, H - 55, "CARD STATEMENT")
    c.setFont("Helvetica", 9)
    c.drawString(50, H - 72, "Statement date: 31 August 2026")

    y = H - 110
    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, y, "Date")
    c.drawString(120, y, "Transaction")
    _draw_right(c, 500, y, "Amount")
    c.line(50, y - 4, 500, y - 4)

    rows = [
        ("02 Aug", "AMZN MKTP US*Z12AB3", "87.42"),
        ("07 Aug", "UBER EATS", "32.40"),
        ("14 Aug", "NETFLIX.COM", "15.99"),
        ("21 Aug", "SHELL OIL 5744221100", "52.10"),
    ]
    c.setFont("Helvetica", 9)
    y -= 20
    for date, desc, amount in rows:
        c.drawString(50, y, date)
        c.drawString(120, y, desc)
        _draw_right(c, 500, y, amount)
        y -= 16
    c.save()
    return buf.getvalue()


def two_pages() -> bytes:
    """Table continues across a page break, with headers repeated."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    def header(page_no):
        c.setFont("Helvetica-Bold", 12)
        c.drawString(50, H - 55, "BIG BANK PLC")
        c.setFont("Helvetica", 8)
        c.drawString(50, H - 68, f"Page {page_no} of 2")
        yy = H - 100
        c.setFont("Helvetica-Bold", 9)
        c.drawString(50, yy, "Date")
        c.drawString(130, yy, "Details")
        _draw_right(c, 430, yy, "Paid out")
        _draw_right(c, 520, yy, "Paid in")
        c.line(50, yy - 4, 520, yy - 4)
        return yy - 20

    page1 = [("01/08/2026", "SALARY PAYMENT", None, "2,400.00"),
             ("04/08/2026", "TESCO STORES 3421", "64.20", None),
             ("06/08/2026", "COUNCIL TAX DD", "180.00", None)]
    page2 = [("18/08/2026", "SAINSBURYS SACAT 1121", "43.15", None),
             ("22/08/2026", "REFUND ARGOS", None, "29.99"),
             ("27/08/2026", "GYM MEMBERSHIP", "38.00", None)]

    for page_no, rows in ((1, page1), (2, page2)):
        y = header(page_no)
        c.setFont("Helvetica", 9)
        for date, desc, out, inn in rows:
            c.drawString(50, y, date)
            c.drawString(130, y, desc)
            if out:
                _draw_right(c, 430, y, out)
            if inn:
                _draw_right(c, 520, y, inn)
            y -= 16
        if page_no == 1:
            c.showPage()
    c.save()
    return buf.getvalue()


def scanned_like_no_text() -> bytes:
    """A page with no extractable text, standing in for a scanned statement."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.rect(60, H - 300, 400, 200, fill=0)
    c.line(60, H - 200, 460, H - 200)
    c.save()
    return buf.getvalue()
