from datetime import date

from app.parsing.amounts import infer_dayfirst, parse_amount, parse_date


def test_plain_amounts():
    assert parse_amount("123.45") == 12345
    assert parse_amount("-123.45") == -12345
    assert parse_amount("0.99") == 99
    assert parse_amount(12.5) == 1250
    assert parse_amount(-3) == -300


def test_thousands_and_decimal_styles():
    assert parse_amount("1,234.56") == 123456
    assert parse_amount("1.234,56") == 123456
    assert parse_amount("1 234,56") == 123456
    assert parse_amount("12,50") == 1250
    assert parse_amount("1,234") == 123400
    assert parse_amount("1.234") == 123400          # European thousands
    assert parse_amount("1.234.567") == 123456700
    assert parse_amount("$2,000.00") == 200000
    assert parse_amount("€45,00") == 4500


def test_negative_notations():
    assert parse_amount("(123.45)") == -12345
    assert parse_amount("123.45-") == -12345
    assert parse_amount("- 55.00") == -5500
    assert parse_amount("12.00 DR") == -1200
    assert parse_amount("12.00 CR") == 1200


def test_non_amounts():
    assert parse_amount("") is None
    assert parse_amount("hello") is None
    assert parse_amount(None) is None
    assert parse_amount("--") is None


def test_text_containing_digits_is_not_an_amount():
    """Regression: digits were harvested out of descriptions, which let a
    description column be chosen as the amount column and produced amounts in
    the hundreds of septillions."""
    for text in ("82305096151500049323763 AMAZONMARK*BF1B08JA1 SEATTLE WA",
                 "REF 12345", "AMAZON MARK* BF1B08JA1", "STOP & SHOP 0049",
                 "CVS/PHARMACY #00107 NEWTONVILLE MA", "INTEREST CHARGE-PURCHASES",
                 "Acct ending 3091", "12 Main St"):
        assert parse_amount(text) is None, text


def test_currency_codes_and_symbols_are_tolerated():
    assert parse_amount("USD 12.34") == 1234
    assert parse_amount("12,34 EUR") == 1234
    assert parse_amount("£1,234.56") == 123456
    assert parse_amount("1'234.56") == 123456        # Swiss grouping


def test_cents_are_exact_for_large_values():
    """Money is parsed with Decimal, so nothing is lost to binary floats."""
    assert parse_amount("8230509615150004.93") == 823050961515000493
    assert parse_amount("0.145") == 15                # half-up, not banker's


def test_dates():
    assert parse_date("2026-08-03") == date(2026, 8, 3)
    assert parse_date("20260803") == date(2026, 8, 3)
    assert parse_date("08/03/2026") == date(2026, 8, 3)
    assert parse_date("03/08/2026", dayfirst=True) == date(2026, 8, 3)
    assert parse_date("3 Aug 2026") == date(2026, 8, 3)
    assert parse_date("Aug 3, 2026") == date(2026, 8, 3)
    assert parse_date("not a date") is None
    assert parse_date("") is None


def test_infer_dayfirst():
    assert infer_dayfirst(["13/05/2026", "01/06/2026"]) is True
    assert infer_dayfirst(["05/13/2026", "06/01/2026"]) is False
    assert infer_dayfirst(["05/06/2026"]) is None
