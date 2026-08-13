"""Statement upload → mapping preview → commit, plus import history/undo."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from .. import config
from ..db import utcnow
from ..deps import current_user, get_conn, render, verify_csrf
from ..parsing.csv_parser import Mapping, apply_mapping
from ..parsing.statements import SUPPORTED_EXTENSIONS, load_statement
from ..services import importer, splits, transfers

router = APIRouter()


def _accounts(conn):
    return conn.execute(
        "SELECT id, name, type FROM accounts WHERE archived = 0 ORDER BY name").fetchall()


@router.get("/import")
def import_page(request: Request, conn=Depends(get_conn), user=Depends(current_user)):
    return render(request, conn, "import_upload.html", accounts=_accounts(conn),
                  supported=", ".join(SUPPORTED_EXTENSIONS), error=None)


def _resolve_account(conn, account_id: str, new_account_name: str,
                     new_account_type: str) -> int:
    if account_id == "new":
        name = new_account_name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="New account needs a name")
        if new_account_type not in ("checking", "savings", "credit", "cash", "other"):
            new_account_type = "checking"
        cur = conn.execute(
            "INSERT INTO accounts (name, type, created_at) VALUES (?, ?, ?)",
            (name, new_account_type, utcnow()))
        conn.commit()
        return cur.lastrowid
    return int(account_id)


def _column_options(stmt) -> list[dict]:
    if not stmt.rows:
        return []
    width = max(len(r) for r in stmt.rows)
    header = stmt.rows[stmt.mapping.header_row] if stmt.mapping.header_row is not None else None
    start = (stmt.mapping.header_row + 1) if stmt.mapping.header_row is not None else 0
    sample_row = stmt.rows[start] if start < len(stmt.rows) else []
    options = []
    for i in range(width):
        name = str(header[i]) if header and i < len(header) and str(header[i]).strip() \
            else f"Column {i + 1}"
        sample = str(sample_row[i])[:24] if i < len(sample_row) else ""
        options.append({"index": i, "label": f"{name}" + (f" — e.g. “{sample}”" if sample else "")})
    return options


def _render_preview(request, conn, token: str, filename: str, account_id: int,
                    stmt, saved_profile: bool):
    stats = importer.preview_stats(conn, account_id, stmt.parsed)
    sample = [r for r in stmt.parsed if not r.error][:12]
    errors = [r for r in stmt.parsed if r.error][:5]
    account = conn.execute("SELECT * FROM accounts WHERE id = ?",
                           (account_id,)).fetchone()
    suggest_flip = False
    if account and account["type"] == "credit" and stmt.kind == "table" \
            and not (stmt.mapping and stmt.mapping.flip_sign):
        ok_rows = [r for r in stmt.parsed if not r.error]
        if ok_rows:
            positive = sum(1 for r in ok_rows if r.amount_cents > 0)
            suggest_flip = positive / len(ok_rows) > 0.7
    return render(request, conn, "import_preview.html",
                  token=token, filename=filename, account=account,
                  kind=stmt.kind, mapping=stmt.mapping,
                  columns=_column_options(stmt), stats=stats, sample=sample,
                  errors=errors, saved_profile=saved_profile,
                  suggest_flip=suggest_flip,
                  is_pdf=filename.lower().endswith(".pdf"))


@router.post("/import/upload", dependencies=[Depends(verify_csrf)])
async def import_upload(request: Request, conn=Depends(get_conn),
                        user=Depends(current_user), file: UploadFile = None,
                        account_id: str = Form(...),
                        new_account_name: str = Form(""),
                        new_account_type: str = Form("checking")):
    if file is None or not file.filename:
        return render(request, conn, "import_upload.html", accounts=_accounts(conn),
                      supported=", ".join(SUPPORTED_EXTENSIONS),
                      error="Choose a statement file to upload.")
    data = await file.read()
    if len(data) > config.MAX_UPLOAD_BYTES:
        limit_mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
        return render(request, conn, "import_upload.html", accounts=_accounts(conn),
                      supported=", ".join(SUPPORTED_EXTENSIONS),
                      error=f"File is too large ({limit_mb} MB max).")
    acct_id = _resolve_account(conn, account_id, new_account_name, new_account_type)
    try:
        stmt = load_statement(file.filename, data)
    except ValueError as e:
        return render(request, conn, "import_upload.html", accounts=_accounts(conn),
                      supported=", ".join(SUPPORTED_EXTENSIONS), error=str(e))

    saved_profile = False
    if stmt.kind == "table":
        profile = importer.load_profile(conn, acct_id, stmt.header_sig)
        if profile is not None:
            profile.header_row = stmt.mapping.header_row  # position is file-specific
            stmt = load_statement(file.filename, data, mapping=profile)
            saved_profile = True
    token = importer.stash_pending(file.filename, data, acct_id)
    return _render_preview(request, conn, token, file.filename, acct_id, stmt,
                           saved_profile)


def _mapping_from_form(form) -> Mapping:
    def col(name):
        v = form.get(name, "")
        return int(v) if str(v).lstrip("-").isdigit() and int(v) >= 0 else None
    desc_cols = [c for c in (col("desc_col1"), col("desc_col2")) if c is not None]
    header_row = form.get("header_row", "")
    m = Mapping(
        date_col=col("date_col") or 0,
        desc_cols=desc_cols,
        flip_sign=bool(form.get("flip_sign")),
        dayfirst=bool(form.get("dayfirst")),
        header_row=int(header_row) if str(header_row).isdigit() else None,
    )
    if form.get("amount_mode") == "split":
        m.debit_col, m.credit_col = col("debit_col"), col("credit_col")
    else:
        m.amount_col = col("amount_col")
    return m


@router.post("/import/commit", dependencies=[Depends(verify_csrf)])
async def import_commit(request: Request, conn=Depends(get_conn),
                        user=Depends(current_user)):
    form = await request.form()
    token = str(form.get("token", ""))
    pending = importer.load_pending(token)
    if pending is None:
        raise HTTPException(status_code=410, detail="Upload expired — please upload the file again.")
    filename, data, account_id = pending

    stmt = load_statement(filename, data)
    if stmt.kind == "table":
        mapping = _mapping_from_form(form)
        if mapping.amount_col is None and mapping.debit_col is None \
                and mapping.credit_col is None:
            raise HTTPException(status_code=400, detail="Pick an amount column.")
        stmt.mapping = mapping
        stmt.parsed = apply_mapping(stmt.rows, mapping)

    if form.get("action") == "refresh":
        return _render_preview(request, conn, token, filename, account_id, stmt,
                               saved_profile=False)

    result = importer.commit_import(conn, account_id, filename, data, stmt.parsed,
                                    user["id"])
    if stmt.kind == "table":
        importer.save_profile(conn, account_id, stmt.header_sig, stmt.mapping)
    importer.drop_pending(token)
    # Now that both sides may be present, match up internal movements.
    linked = transfers.auto_link(conn)
    uncat = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL").fetchone()[0]
    return render(request, conn, "import_result.html", result=result,
                  filename=filename, uncat=uncat, linked=linked,
                  transfer_suggestions=transfers.suggestion_count(conn),
                  to_confirm=splits.pending_confirmation_count(conn))


@router.get("/imports")
def imports_history(request: Request, conn=Depends(get_conn),
                    user=Depends(current_user)):
    rows = conn.execute(
        "SELECT i.*, a.name AS account_name, u.display_name AS uploader "
        "FROM imports i JOIN accounts a ON a.id = i.account_id "
        "LEFT JOIN users u ON u.id = i.uploaded_by "
        "ORDER BY i.id DESC LIMIT 100").fetchall()
    return render(request, conn, "imports_history.html", rows=rows)


@router.post("/imports/{import_id}/delete", dependencies=[Depends(verify_csrf)])
def import_delete(import_id: int, request: Request, conn=Depends(get_conn),
                  user=Depends(current_user)):
    importer.delete_import(conn, import_id)
    return RedirectResponse("/imports", status_code=303)
