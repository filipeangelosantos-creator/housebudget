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
                            files={"file": ("sample-checking.csv", f, "text/csv")})
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
                            files={"file": ("sample-checking.csv", f, "text/csv")})
        assert "Recognized this bank" in r.text  # saved import profile hit
        assert re.search(r"New transactions</td><td[^>]*><strong>0</strong>", r.text)

        # Dashboard for the statement month
        r = client.get("/?month=2026-08")
        assert "Groceries" in r.text          # auto-categorized spending visible
        assert "need" in r.text               # uncategorized banner (Luigi's Deli)

        # Review queue: categorize Luigi's and learn a rule
        r = client.get("/review")
        assert "LUIGI" in r.text
        txn_id = re.search(r'action="/review/(\d+)"', r.text).group(1)
        r = client.post(f"/review/{txn_id}", data={
            "csrf": get_csrf(r.text),
            "category_id": str(cat_id("Restaurants & Takeout")),
            "remember": "1", "pattern": "LUIGIS DELI"})
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

        # Every remaining page renders
        conn = db.connect(config.DB_PATH)
        txn = conn.execute("SELECT id FROM transactions LIMIT 1").fetchone()
        conn.close()
        for path in ("/transactions", "/transactions?month=all&q=star",
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
