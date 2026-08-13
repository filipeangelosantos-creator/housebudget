"""Shared web-layer plumbing: DB/request dependencies, template environment,
auth guard and CSRF verification."""
from pathlib import Path

from fastapi import Depends, Form, HTTPException, Request
from fastapi.templating import Jinja2Templates

from . import auth, db
from .parsing.amounts import parse_amount

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def money(cents) -> str:
    """1234567 -> '12,345.67' (absolute value; sign handled by callers)."""
    return f"{abs(int(cents or 0)) / 100:,.2f}"


def money_signed(cents) -> str:
    v = int(cents or 0)
    return ("-" if v < 0 else "") + money(abs(v))


def money_plain(cents) -> str:
    """For form inputs: 1234.56 without separators; empty for zero."""
    v = int(cents or 0)
    return "" if v == 0 else f"{v / 100:.2f}"


MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def day_month(d) -> str:
    """A date as '7 Aug'.

    Built by hand rather than with strftime. The directive for a day with no
    leading zero is a glibc extension: it prints "7" on Linux and raises
    ValueError on Windows, so a page that renders fine here is an Internal
    Server Error there. Month names are spelled out for the same reason %b
    isn't used — it follows the machine's locale, and the rest of the app
    names months in English.
    """
    return f"{d.day} {MONTH_ABBR[d.month - 1]}"


def day_month_year(d) -> str:
    """A date as '31 Jan 2027'.

    For anything that can fall outside the month on screen — when a
    twice-yearly bill is next due, say — the year is the difference between a
    date and a guess.
    """
    return f"{day_month(d)} {d.year}"


templates.env.filters["money"] = money
templates.env.filters["money_signed"] = money_signed
templates.env.filters["money_plain"] = money_plain
templates.env.filters["day_month"] = day_month
templates.env.filters["day_month_year"] = day_month_year


def parse_money_input(text: str) -> int:
    """User-entered budget/amount ('1.234,56', '1234.56', '') -> cents >= 0 handling."""
    cents = parse_amount(text)
    return cents if cents is not None else 0


class RequiresLogin(Exception):
    def __init__(self, next_path: str = "/"):
        self.next_path = next_path


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn=Depends(get_conn)):
    session = auth.read_session(request)
    if session is None:
        raise RequiresLogin(request.url.path)
    row = conn.execute("SELECT * FROM users WHERE id = ?",
                       (session["uid"],)).fetchone()
    if row is None:
        raise RequiresLogin(request.url.path)
    request.state.csrf = session.get("csrf", "")
    return row


def verify_csrf(request: Request, csrf: str = Form("")):
    if not auth.check_csrf(request, csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token. Go back, reload the page and try again.")


def render(request: Request, conn, template: str, **ctx):
    ctx.setdefault("currency", db.get_setting(conn, "currency", "$"))
    ctx.setdefault("household", db.get_setting(conn, "household", "Our Budget"))
    ctx.setdefault("csrf", getattr(request.state, "csrf", ""))
    ctx.setdefault("active", "/" + request.url.path.strip("/").split("/")[0]
                   if request.url.path != "/" else "/")
    return templates.TemplateResponse(request, template, ctx)
