"""End-to-end journey through the web app (setup → import → review → budget)."""
import re

from fastapi.testclient import TestClient

from app import config, db
from app.main import app
from tests.conftest import SAMPLES


def get_csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def cat_id(name: str) -> int:
    conn = db.connect(config.DB_PATH)
    try:
        return conn.execute("SELECT id FROM categories WHERE name = ?",
                            (name,)).fetchone()["id"]
    finally:
        conn.close()


def test_full_journey():
    with TestClient(app) as client:
        # No users yet: everything funnels to setup
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303
        r = client.get("/login")
        assert r.url.path == "/setup"

        # Create both users
        r = client.post("/setup", data={
            "username": "filipe", "display_name": "Filipe",
            "password": "verysecret1", "password2": "verysecret1",
            "username_p": "ana", "display_name_p": "Ana",
            "password_p": "alsosecret2"})
        assert r.url.path == "/"

        # Upload a statement into a new account
        r = client.get("/import")
        csrf = get_csrf(r.text)
        with open(SAMPLES / "sample-checking.csv", "rb") as f:
            r = client.post("/import/upload",
                            data={"csrf": csrf, "account_id": "new",
                                  "new_account_name": "Joint Checking",
                                  "new_account_type": "checking"},
                            files={"files": ("sample-checking.csv", f, "text/csv")})
        assert r.status_code == 200
        assert "Check before importing" in r.text
        token = re.search(r'name="token" value="([a-f0-9]+)"', r.text).group(1)

        # Confirm with the guessed mapping
        r = client.post("/import/commit", data={
            "csrf": get_csrf(r.text), "token": token, "action": "confirm",
            "header_row": "0", "date_col": "0", "desc_col1": "1",
            "amount_mode": "single", "amount_col": "2"})
        assert "Import complete" in r.text
        assert "<strong>14</strong>" in r.text

        # Re-import: everything is a duplicate
        r = client.get("/import")
        with open(SAMPLES / "sample-checking.csv", "rb") as f:
            r = client.post("/import/upload",
                            data={"csrf": get_csrf(r.text), "account_id": "1"},
                            files={"files": ("sample-checking.csv", f, "text/csv")})
        assert "Recognized this bank" in r.text  # saved import profile hit
        assert re.search(r"New transactions</td><td[^>]*><strong>0</strong>", r.text)

        # Dashboard for the statement month
        r = client.get("/?month=2026-08")
        assert "Groceries" in r.text          # auto-categorized spending visible
        assert "need" in r.text               # uncategorized banner (Luigi's Deli)

        # Review queue: categorize Luigi's via the per-row save and learn a rule
        r = client.get("/review")
        assert "LUIGI" in r.text
        txn_id = re.search(r'name="cat_(\d+)"', r.text).group(1)
        r = client.post("/review/save", data={
            "csrf": get_csrf(r.text), "only": txn_id,
            f"cat_{txn_id}": str(cat_id("Restaurants & Takeout")),
            f"remember_{txn_id}": "1", f"pattern_{txn_id}": "LUIGIS DELI"})
        conn = db.connect(config.DB_PATH)
        rule = conn.execute("SELECT * FROM rules WHERE pattern = 'LUIGIS DELI'").fetchone()
        assert rule is not None and rule["source"] == "learned"
        conn.close()

        # Budgets: save and read back
        r = client.get("/budgets?month=2026-08")
        r = client.post("/budgets/save", data={
            "csrf": get_csrf(r.text), "month": "2026-08",
            f"cat_{cat_id('Groceries')}": "500"})
        r = client.get("/budgets?month=2026-08")
        assert 'value="500.00"' in r.text
        r = client.get("/?month=2026-08")
        assert "/ $500.00" in r.text          # budget bar amounts

        # Insights, export, backup
        r = client.get("/insights?month=2026-08")
        for section in ("Spending pace", "Surplus &amp; deficit",
                        "Income vs spending", "Where it goes", "Top merchants"):
            assert section in r.text, f"missing insights section: {section}"
        # charts carry real numbers, not silently-empty template lookups
        assert re.search(r'legend-val">\$(?!0\.00)[\d,]+\.\d\d', r.text), \
            "composition legend rendered no values"
        assert "NaN" not in r.text
        r = client.get("/export.csv")
        assert r.status_code == 200 and "WALMART" in r.text
        r = client.get("/backup.db")
        assert r.status_code == 200 and len(r.content) > 1000

        # The Costco charge is at a mixed-basket merchant: auto-categorized as
        # Groceries by rule, but flagged for confirmation rather than assumed.
        conn = db.connect(config.DB_PATH)
        costco = conn.execute(
            "SELECT id, category_id, needs_review, amount_cents FROM transactions "
            "WHERE description LIKE '%COSTCO%'").fetchone()
        conn.close()
        assert costco["needs_review"] == 1
        assert costco["category_id"] == cat_id("Groceries")

        r = client.get("/review")
        assert "Worth a look" in r.text and "COSTCO" in r.text

        # Split it: 150.00 groceries + 81.80 clothing = 231.80
        r = client.get(f"/transactions/{costco['id']}/split?back=/review")
        assert r.status_code == 200
        r = client.post(f"/transactions/{costco['id']}/split", data={
            "csrf": get_csrf(r.text), "back": "/review",
            "cat_00": str(cat_id("Groceries")), "amt_00": "150.00",
            "cat_01": str(cat_id("Clothing")), "amt_01": "81.80"})
        assert r.url.path == "/review"

        conn = db.connect(config.DB_PATH)
        parts = conn.execute(
            "SELECT category_id, amount_cents FROM transaction_splits "
            "WHERE transaction_id = ? ORDER BY sort_order", (costco["id"],)).fetchall()
        alloc = conn.execute(
            "SELECT SUM(amount_cents) AS t FROM txn_allocations WHERE txn_id = ?",
            (costco["id"],)).fetchone()["t"]
        still_flagged = conn.execute(
            "SELECT needs_review FROM transactions WHERE id = ?",
            (costco["id"],)).fetchone()["needs_review"]
        conn.close()
        assert [(p["category_id"], p["amount_cents"]) for p in parts] == [
            (cat_id("Groceries"), -15000), (cat_id("Clothing"), -8180)]
        assert alloc == costco["amount_cents"]      # parts still total the charge
        assert still_flagged == 0

        # A split that doesn't add up is rejected and explains itself
        r = client.get(f"/transactions/{costco['id']}/split")
        r = client.post(f"/transactions/{costco['id']}/split", data={
            "csrf": get_csrf(r.text), "back": "/transactions",
            "cat_00": str(cat_id("Groceries")), "amt_00": "10.00",
            "cat_01": str(cat_id("Clothing")), "amt_01": "10.00"})
        assert "off by" in r.text

        # Splitting is visible on the dashboard budget bars
        r = client.get("/?month=2026-08")
        assert "Clothing" in r.text

        # Batch save: two manual uncategorized transactions from one merchant,
        # both saved in a single submit; remember on one creates one rule.
        r = client.get("/transactions/new")
        csrf = get_csrf(r.text)
        for day, amt in (("2026-08-03", "12.00"), ("2026-08-04", "15.00")):
            client.post("/transactions/new", data={
                "csrf": csrf, "account_id": "1", "txn_date": day,
                "amount": amt, "direction": "expense",
                "description": "CORNER BAKERY 774"})
        r = client.get("/review")
        ids = re.findall(r'name="cat_(\d+)"', r.text)
        assert len(ids) >= 2
        payload = {"csrf": get_csrf(r.text), "only": "all"}
        for i in ids:
            payload[f"cat_{i}"] = str(cat_id("Coffee & Snacks"))
            payload[f"pattern_{i}"] = "CORNER BAKERY"
        payload[f"remember_{ids[0]}"] = "1"
        r = client.post("/review/save", data=payload)
        conn = db.connect(config.DB_PATH)
        left = conn.execute("SELECT COUNT(*) FROM transactions "
                            "WHERE category_id IS NULL").fetchone()[0]
        nrules = conn.execute("SELECT COUNT(*) FROM rules "
                              "WHERE pattern = 'CORNER BAKERY'").fetchone()[0]
        conn.close()
        assert left == 0
        assert nrules == 1

        # Multiple files in one upload: previewed one at a time, then a
        # combined summary. The second file overlaps the first on purpose.
        r = client.get("/import")
        csrf = get_csrf(r.text)
        with open(SAMPLES / "sample-checking.csv", "rb") as f1, \
                open(SAMPLES / "sample-visa.csv", "rb") as f2:
            r = client.post("/import/upload",
                            data={"csrf": csrf, "account_id": "new",
                                  "new_account_name": "Batch Card",
                                  "new_account_type": "credit"},
                            files=[("files", ("a-checking.csv", f1, "text/csv")),
                                   ("files", ("b-visa.csv", f2, "text/csv"))])
        assert "File 1 of 2" in r.text
        batch = re.search(r'name="batch" value="([a-f0-9]+)"', r.text).group(1)
        token = re.search(r'name="token" value="([a-f0-9]+)"', r.text).group(1)
        r = client.post("/import/commit", data={
            "csrf": get_csrf(r.text), "token": token, "batch": batch,
            "action": "confirm", "header_row": "0", "date_col": "0",
            "desc_col1": "1", "amount_mode": "single", "amount_col": "2"})
        # straight on to the next file, no return trip to the upload form
        assert "File 2 of 2" in r.text
        token2 = re.search(r'name="token" value="([a-f0-9]+)"', r.text).group(1)
        assert token2 != token
        r = client.post("/import/commit", data={
            "csrf": get_csrf(r.text), "token": token2, "batch": batch,
            "action": "confirm", "header_row": "0", "date_col": "0",
            "desc_col1": "1", "amount_mode": "single", "amount_col": "2"})
        assert "Import complete" in r.text
        assert "2 files imported" in r.text
        assert "a-checking.csv" in r.text and "b-visa.csv" in r.text

        # Accounts: the duplicate can be merged away and nothing is doubled
        r = client.get("/accounts")
        assert "Merge into another account" in r.text
        conn = db.connect(config.DB_PATH)
        batch_acct = conn.execute(
            "SELECT id FROM accounts WHERE name = 'Batch Card'").fetchone()["id"]
        before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.close()
        r = client.post(f"/accounts/{batch_acct}/merge",
                        data={"csrf": get_csrf(r.text), "target_id": "1"})
        assert "Merged" in r.text
        conn = db.connect(config.DB_PATH)
        after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        gone = conn.execute("SELECT COUNT(*) FROM accounts WHERE id = ?",
                            (batch_acct,)).fetchone()[0]
        conn.close()
        assert gone == 0
        assert after < before          # the overlapping checking rows collapsed

        # Every remaining page renders
        conn = db.connect(config.DB_PATH)
        txn = conn.execute("SELECT id FROM transactions LIMIT 1").fetchone()
        conn.close()
        for path in ("/transactions", "/transactions?month=all&q=star",
                     # the filter form submits empty values for "all" — these
                     # 422'd instead of rendering (regression)
                     "/transactions?month=2026-06&account=&category=&q=",
                     "/transactions?account=999&page=&q=%25",
                     "/transactions?page=abc&account=abc",
                     "/transactions/new", f"/transactions/{txn['id']}",
                     f"/transactions/{txn['id']}/split",
                     "/accounts", "/categories", "/rules", "/settings",
                     "/imports", "/review", "/transfers", "/healthz"):
            r = client.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_auth_and_csrf_guards():
    with TestClient(app) as client:
        # not signed in -> redirected to login
        r = client.get("/transactions", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")

        # sign in (users exist from the journey test)
        r = client.post("/login", data={"username": "ana", "password": "alsosecret2"})
        assert r.url.path == "/"

        # POST without csrf is rejected
        r = client.post("/rules/add", data={"pattern": "X", "category_id": "1"})
        assert r.status_code == 403

        # wrong password rejected
        fresh = TestClient(app)
        r = fresh.post("/login", data={"username": "ana", "password": "wrongwrong"})
        assert r.status_code == 401
