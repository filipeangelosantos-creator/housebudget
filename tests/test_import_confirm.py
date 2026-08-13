"""Confirming what an import is about to save — amounts and their direction.

A statement that lists deposits and withdrawals under separate headings keeps
the direction in the heading, not in the number. Miss the heading and every
deposit arrives as a withdrawal: a payroll credit of 2,079.43 lands as money
out, and the month is wrong by twice the paycheque. The parser should get this
right, and when it doesn't there has to be somewhere to say so.
"""
import re

import pytest

from app import config, db


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


def get_csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf token"
    return m.group(1)


STATEMENT = (b"Date,Description,Amount\n"
             b"2026-08-05,HSBCBK CK WEBXFR P2P 260805 FILIPE,4000.00\n"
             b"2026-08-06,THE BOSTON CONSU PAYROLL 260807,2079.43\n"
             b"2026-08-04,AMEX EPAYMENT ACH PMT 260804,-3389.55\n")

MAPPING = {"header_row": "0", "date_col": "0", "desc_col1": "1",
           "amount_mode": "single", "amount_col": "2"}


def upload(client, data=STATEMENT, name="citizens.csv"):
    r = client.get("/import")
    r = client.post("/import/upload",
                    data={"csrf": get_csrf(r.text), "account_id": "new",
                          "new_account_name": "Citizens Checking",
                          "new_account_type": "checking"},
                    files={"files": (name, data, "text/csv")})
    token = re.search(r'name="token" value="([a-f0-9]+)"', r.text).group(1)
    return r, token


def stored():
    conn = db.connect(config.DB_PATH)
    rows = {r["description"][:20]: r["amount_cents"] for r in
            conn.execute("SELECT description, amount_cents FROM transactions")}
    conn.close()
    return rows


# --- the preview offers every row, not a sample -------------------------------

def test_every_row_can_be_confirmed_not_just_the_first_few(signed_in):
    r, _ = upload(signed_in)
    assert "Check what's being imported (3)" in r.text
    assert r.text.count('name="keep_') == 3
    assert r.text.count('name="dir_') == 3
    assert r.text.count('name="amt_') == 3


def test_the_direction_starts_where_the_file_put_it(signed_in):
    r, _ = upload(signed_in)
    rows = re.findall(r'<option value="out"( selected)?>out</option>', r.text)
    # the third row is the only negative one in the file
    assert [bool(s) for s in rows] == [False, False, True]


# --- fixing one row -----------------------------------------------------------

def test_a_row_can_have_its_direction_turned_round(signed_in):
    """The reported case: a payroll credit imported as money out."""
    r, token = upload(signed_in)
    signed_in.post("/import/commit", data={
        "csrf": get_csrf(r.text), "token": token, "action": "confirm", **MAPPING,
        "keep_0": "1", "amt_0": "4000.00", "dir_0": "in",
        "keep_1": "1", "amt_1": "2079.43", "dir_1": "out",   # override to out
        "keep_2": "1", "amt_2": "3389.55", "dir_2": "out"})

    got = stored()
    assert got["HSBCBK CK WEBXFR P2P"] == 400000
    assert got["THE BOSTON CONSU PAY"] == -207943      # what you said, not the file
    assert got["AMEX EPAYMENT ACH PM"] == -338955


def test_a_row_can_have_its_amount_corrected(signed_in):
    r, token = upload(signed_in)
    signed_in.post("/import/commit", data={
        "csrf": get_csrf(r.text), "token": token, "action": "confirm", **MAPPING,
        "keep_0": "1", "amt_0": "4,000.00", "dir_0": "in",
        "keep_1": "1", "amt_1": "2079.43", "dir_1": "in",
        "keep_2": "1", "amt_2": "1234.56", "dir_2": "out"})
    assert stored()["AMEX EPAYMENT ACH PM"] == -123456


def test_an_unwanted_row_can_be_left_out(signed_in):
    r, token = upload(signed_in)
    signed_in.post("/import/commit", data={
        "csrf": get_csrf(r.text), "token": token, "action": "confirm", **MAPPING,
        "keep_0": "1", "amt_0": "4000.00", "dir_0": "in",
        "keep_2": "1", "amt_2": "3389.55", "dir_2": "out"})     # row 1 unticked

    got = stored()
    assert "THE BOSTON CONSU PAY" not in got
    assert len(got) == 2


def test_a_blank_amount_leaves_the_row_as_the_file_had_it(signed_in):
    """An empty box is not an instruction to import nothing."""
    r, token = upload(signed_in)
    signed_in.post("/import/commit", data={
        "csrf": get_csrf(r.text), "token": token, "action": "confirm", **MAPPING,
        "keep_0": "1", "amt_0": "", "dir_0": "in",
        "keep_1": "1", "amt_1": "2079.43", "dir_1": "in",
        "keep_2": "1", "amt_2": "3389.55", "dir_2": "out"})
    assert stored()["HSBCBK CK WEBXFR P2P"] == 400000


# --- fixing the whole file ----------------------------------------------------

def test_a_whole_file_the_wrong_way_round_can_be_flipped_at_once(signed_in):
    """Thirty rows out of one missed heading is one switch, not thirty edits."""
    r, token = upload(signed_in)
    r2 = signed_in.post("/import/commit", data={
        "csrf": get_csrf(r.text), "token": token, "action": "refresh",
        **MAPPING, "flip_all": "1"})
    assert "checked" in re.search(r'name="flip_all"[^>]*>', r2.text).group(0)

    token2 = re.search(r'name="token" value="([a-f0-9]+)"', r2.text).group(1)
    signed_in.post("/import/commit", data={
        "csrf": get_csrf(r2.text), "token": token2, "action": "confirm",
        **MAPPING, "flip_all": "1"})

    got = stored()
    assert got["HSBCBK CK WEBXFR P2P"] == -400000      # was +4000 in the file
    assert got["AMEX EPAYMENT ACH PM"] == 338955       # was -3389.55


def test_a_row_edit_beats_the_flip(signed_in):
    """You looked at that row last, so it wins."""
    r, token = upload(signed_in)
    signed_in.post("/import/commit", data={
        "csrf": get_csrf(r.text), "token": token, "action": "confirm",
        **MAPPING, "flip_all": "1",
        "keep_0": "1", "amt_0": "4000.00", "dir_0": "in",
        "keep_1": "1", "amt_1": "2079.43", "dir_1": "in",
        "keep_2": "1", "amt_2": "3389.55", "dir_2": "out"})

    got = stored()
    assert got["HSBCBK CK WEBXFR P2P"] == 400000
    assert got["AMEX EPAYMENT ACH PM"] == -338955


def test_the_flip_option_is_offered_for_a_pdf_too(signed_in):
    """It used to live in the column-mapping panel, which a PDF never shows —
    so a PDF with every sign backwards had no recourse at all."""
    r, _ = upload(signed_in)
    assert "flip the lot" in r.text
    assert 'name="flip_all"' in r.text


def test_confirming_without_touching_anything_imports_the_file_as_read(signed_in):
    r, token = upload(signed_in)
    signed_in.post("/import/commit", data={
        "csrf": get_csrf(r.text), "token": token, "action": "confirm", **MAPPING,
        "keep_0": "1", "amt_0": "4000.00", "dir_0": "in",
        "keep_1": "1", "amt_1": "2079.43", "dir_1": "in",
        "keep_2": "1", "amt_2": "3389.55", "dir_2": "out"})
    assert stored() == {"HSBCBK CK WEBXFR P2P": 400000,
                        "THE BOSTON CONSU PAY": 207943,
                        "AMEX EPAYMENT ACH PM": -338955}


def test_the_amount_box_holds_a_size_and_the_select_holds_the_sign(signed_in):
    """A minus in the box beside a direction saying "out" reads as a double
    negative. One of them owns the sign, and it is the select."""
    r, _ = upload(signed_in)
    assert 'name="amt_2"' in r.text
    box = re.search(r'name="amt_2"[^>]*value="([^"]*)"', r.text).group(1)
    assert box == "3389.55"                    # not -3389.55
