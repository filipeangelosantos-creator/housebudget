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
        assert "Cashflow" in r.text and "Top merchants" in r.text
        r = client.get("/export.csv")
        assert r.status_code == 200 and "WALMART" in r.text
        r = client.get("/backup.db")
        assert r.status_code == 200 and len(r.content) > 1000

        # Every remaining page renders
        conn = db.connect(config.DB_PATH)
        txn = conn.execute("SELECT id FROM transactions LIMIT 1").fetchone()
        conn.close()
        for path in ("/transactions", "/transactions?month=all&q=star",
                     "/transactions/new", f"/transactions/{txn['id']}",
                     "/accounts", "/categories", "/rules", "/settings",
                     "/imports", "/review", "/healthz"):
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
