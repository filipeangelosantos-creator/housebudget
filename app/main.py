"""HouseBudget — self-hosted household budget vs. actuals.

Run locally:  uvicorn app.main:app --reload
Production:   see Dockerfile / docker-compose.yml
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .deps import RequiresLogin
from .routes import (accounts_routes, auth_routes, budgets_routes,
                     classified_routes, dashboard, imports, insights_routes,
                     rules_routes, settings_routes, transactions,
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
    finally:
        conn.close()
    yield


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
               transfers_routes, classified_routes):
    app.include_router(module.router)
