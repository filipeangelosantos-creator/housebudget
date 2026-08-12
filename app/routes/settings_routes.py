"""Settings: household preferences, users, data export/backup."""
import csv
import io

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from .. import auth, config
from ..db import get_setting, set_setting, utcnow
from ..deps import current_user, get_conn, render, verify_csrf

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request, conn=Depends(get_conn),
                  user=Depends(current_user)):
    users = conn.execute(
        "SELECT id, username, display_name FROM users ORDER BY id").fetchall()
    return render(request, conn, "settings.html", users=users, me=user,
                  message=request.query_params.get("m", ""))


@router.post("/settings/save", dependencies=[Depends(verify_csrf)])
def settings_save(request: Request, conn=Depends(get_conn),
                  user=Depends(current_user), currency: str = Form("$"),
                  household: str = Form("Our Budget")):
    set_setting(conn, "currency", currency.strip()[:5] or "$")
    set_setting(conn, "household", household.strip()[:40] or "Our Budget")
    return RedirectResponse("/settings?m=Saved.", status_code=303)


@router.post("/settings/password", dependencies=[Depends(verify_csrf)])
def change_password(request: Request, conn=Depends(get_conn),
                    user=Depends(current_user), current: str = Form(""),
                    password: str = Form(""), password2: str = Form("")):
    if not auth.verify_password(current, user["password_hash"]):
        return RedirectResponse("/settings?m=Current+password+is+wrong.",
                                status_code=303)
    if len(password) < 8 or password != password2:
        return RedirectResponse(
            "/settings?m=New+passwords+must+match+(8%2B+characters).", status_code=303)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (auth.hash_password(password), user["id"]))
    conn.commit()
    return RedirectResponse("/settings?m=Password+changed.", status_code=303)


@router.post("/settings/adduser", dependencies=[Depends(verify_csrf)])
def add_user(request: Request, conn=Depends(get_conn), user=Depends(current_user),
             username: str = Form(""), display_name: str = Form(""),
             password: str = Form("")):
    username = username.strip()
    if len(username) < 2 or len(password) < 8:
        return RedirectResponse(
            "/settings?m=Username+(2%2B)+and+password+(8%2B+chars)+required.",
            status_code=303)
    exists = conn.execute("SELECT 1 FROM users WHERE username = ?",
                          (username,)).fetchone()
    if exists:
        return RedirectResponse("/settings?m=Username+already+taken.", status_code=303)
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash, created_at) "
        "VALUES (?, ?, ?, ?)",
        (username, display_name.strip() or username, auth.hash_password(password),
         utcnow()))
    conn.commit()
    return RedirectResponse("/settings?m=User+added.", status_code=303)


@router.get("/export.csv")
def export_csv(request: Request, conn=Depends(get_conn), user=Depends(current_user)):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "account", "description", "amount", "category",
                     "group", "notes"])
    rows = conn.execute(
        "SELECT t.date, a.name AS account, t.description, t.amount_cents, "
        "c.name AS category, g.name AS grp, t.notes "
        "FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "LEFT JOIN categories c ON c.id = t.category_id "
        "LEFT JOIN category_groups g ON g.id = c.group_id "
        "ORDER BY t.date, t.id").fetchall()
    for r in rows:
        writer.writerow([r["date"], r["account"], r["description"],
                         f"{r['amount_cents'] / 100:.2f}", r["category"] or "",
                         r["grp"] or "", r["notes"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=housebudget-export.csv"})


@router.get("/backup.db")
def backup_db(request: Request, conn=Depends(get_conn), user=Depends(current_user)):
    # Consistent snapshot even while the app is running (WAL-safe).
    backup_path = config.DATA_DIR / "backup-snapshot.db"
    import sqlite3
    dest = sqlite3.connect(backup_path)
    with dest:
        conn.backup(dest)
    dest.close()
    return FileResponse(backup_path, media_type="application/octet-stream",
                        filename="housebudget-backup.db")
