"""Login/logout and first-run setup (create the household's user accounts)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from .. import auth
from ..db import utcnow
from ..deps import get_conn, render, templates

router = APIRouter()


def _no_users(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def _safe_next(next_path: str) -> str:
    if next_path.startswith("/") and not next_path.startswith("//"):
        return next_path
    return "/"


@router.get("/login")
def login_page(request: Request, conn=Depends(get_conn), next: str = "/"):
    if _no_users(conn):
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "next": _safe_next(next)})


@router.post("/login")
def login_submit(request: Request, conn=Depends(get_conn),
                 username: str = Form(""), password: str = Form(""),
                 next: str = Form("/")):
    ip = auth.client_ip(request)
    if auth.login_locked(ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Too many attempts. Try again in a few minutes.",
             "next": _safe_next(next)}, status_code=429)
    row = conn.execute("SELECT * FROM users WHERE username = ?",
                       (username.strip(),)).fetchone()
    if row is None or not auth.verify_password(password, row["password_hash"]):
        auth.record_login_failure(ip)
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Wrong username or password.", "next": _safe_next(next)},
            status_code=401)
    auth.clear_login_failures(ip)
    response = RedirectResponse(_safe_next(next), status_code=303)
    auth.set_session_cookie(response, request, row["id"])
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookie(response)
    return response


@router.get("/setup")
def setup_page(request: Request, conn=Depends(get_conn)):
    if not _no_users(conn):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@router.post("/setup")
def setup_submit(request: Request, conn=Depends(get_conn),
                 username: str = Form(""), display_name: str = Form(""),
                 password: str = Form(""), password2: str = Form(""),
                 username_p: str = Form(""), display_name_p: str = Form(""),
                 password_p: str = Form("")):
    # Only available while no users exist, so no session/CSRF is required here.
    if not _no_users(conn):
        return RedirectResponse("/login", status_code=303)

    def bad(msg):
        return templates.TemplateResponse(request, "setup.html", {"error": msg},
                                          status_code=400)

    username = username.strip()
    if len(username) < 2:
        return bad("Pick a username (at least 2 characters).")
    if len(password) < 8:
        return bad("Password must be at least 8 characters.")
    if password != password2:
        return bad("Passwords don't match.")
    username_p = username_p.strip()
    if username_p:
        if username_p.lower() == username.lower():
            return bad("The two usernames must be different.")
        if len(password_p) < 8:
            return bad("Partner password must be at least 8 characters.")

    now = utcnow()
    cur = conn.execute(
        "INSERT INTO users (username, display_name, password_hash, created_at) "
        "VALUES (?, ?, ?, ?)",
        (username, display_name.strip() or username, auth.hash_password(password), now))
    user_id = cur.lastrowid
    if username_p:
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username_p, display_name_p.strip() or username_p,
             auth.hash_password(password_p), now))
    conn.commit()

    response = RedirectResponse("/", status_code=303)
    auth.set_session_cookie(response, request, user_id)
    return response
