"""HouseBudget — self-hosted household budget vs. actuals.

Run locally:  uvicorn app.main:app --reload
Production:   see Dockerfile / docker-compose.yml
"""
import getpass
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .deps import RequiresLogin
from .routes import (accounts_routes, auth_routes, budgets_routes,
                     cashflow_routes, classified_routes, dashboard, imports,
                     insights_routes, plan_routes, rules_routes,
                     settings_routes, statements_routes, transactions,
                     transfers_routes)
from .services.seed import seed_defaults

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    conn = db.connect()
    try:
        db.init_db(conn)
        seed_defaults(conn)
        _announce(conn)
    finally:
        conn.close()
    yield


def _announce(conn) -> None:
    """Say which database this is, every time the app starts.

    Where the data lives is resolved from the environment, from whether a
    ./data directory happens to exist, and otherwise from the home directory of
    whoever is running the process — so the same code can open a different file
    depending on which account started it. When that happens the app looks
    factory-fresh rather than broken, which is the worst way for it to fail.
    Printing the path and the row counts turns that into a one-line diagnosis.
    """
    logger = logging.getLogger("housebudget")
    users, txns = 0, 0
    try:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        txns = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    except sqlite3.Error:                       # a database too broken to count
        logger.warning("HouseBudget data: %s (unreadable)", config.DB_PATH)
        return
    logger.warning("HouseBudget data: %s", config.DB_PATH)
    logger.warning("HouseBudget: %d user(s), %d transaction(s), running as %s",
                   users, txns, _whoami())
    if not users:
        logger.warning(
            "HouseBudget: this database is empty, so the app will ask you to "
            "set it up. If you already had one, this is a different file — set "
            "HB_DATA_DIR to the directory holding it and restart.")


def _whoami() -> str:
    """Whoever owns this process, for when a service and a terminal disagree."""
    try:
        return getpass.getuser()
    except Exception:                           # no password database, no USER
        return "unknown"


app = FastAPI(title="HouseBudget", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(RequiresLogin)
async def requires_login_handler(request: Request, exc: RequiresLogin):
    return RedirectResponse(f"/login?next={exc.next_path}", status_code=303)


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")


@app.get("/favicon.ico")
def favicon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
           '<text y=".9em" font-size="90">🏡</text></svg>')
    return PlainTextResponse(svg, media_type="image/svg+xml")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "frame-ancestors 'none'")
    return response


for module in (auth_routes, dashboard, transactions, imports, budgets_routes,
               insights_routes, rules_routes, accounts_routes, settings_routes,
               transfers_routes, classified_routes, plan_routes,
               cashflow_routes, statements_routes):
    app.include_router(module.router)
