"""Statement upload → mapping preview → commit, plus import history/undo."""
from fastapi import (APIRouter, Depends, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import RedirectResponse

from .. import config
from ..db import utcnow
from ..deps import current_user, get_conn, render, verify_csrf
from ..parsing.csv_parser import Mapping, apply_mapping
from ..parsing.statements import SUPPORTED_EXTENSIONS, load_statement
from ..services import importer, splits, transfers

router = APIRouter()


def _accounts(conn):
    # Transaction counts make two same-named accounts distinguishable in the
    # picker, which is otherwise the one place a duplicate is invisible.
    return conn.execute(
        "SELECT a.id, a.name, a.type, COUNT(t.id) AS txn_count "
        "FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id "
        "WHERE a.archived = 0 GROUP BY a.id ORDER BY a.name, a.id").fetchall()


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
                    stmt, saved_profile: bool, batch: str = "",
                    batch_pos: int = 0, batch_total: int = 0):
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
                  is_pdf=filename.lower().endswith(".pdf"),
                  batch=batch, batch_pos=batch_pos, batch_total=batch_total)


@router.post("/import/upload", dependencies=[Depends(verify_csrf)])
async def import_upload(request: Request, conn=Depends(get_conn),
                        user=Depends(current_user),
                        files: list[UploadFile] = File(default=None),
                        account_id: str = Form(...),
                        new_account_name: str = Form(""),
                        new_account_type: str = Form("checking")):
    uploads = [f for f in (files or []) if f is not None and f.filename]
    if not uploads:
        return render(request, conn, "import_upload.html", accounts=_accounts(conn),
                      supported=", ".join(SUPPORTED_EXTENSIONS),
                      error="Choose one or more statement files to upload.")

    limit_mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
    collected: list[tuple[str, bytes]] = []
    for upload in uploads:
        data = await upload.read()
        if len(data) > config.MAX_UPLOAD_BYTES:
            return render(request, conn, "import_upload.html",
                          accounts=_accounts(conn),
                          supported=", ".join(SUPPORTED_EXTENSIONS),
                          error=f"“{upload.filename}” is too large ({limit_mb} MB max).")
        collected.append((upload.filename, data))

    acct_id = _resolve_account(conn, account_id, new_account_name, new_account_type)
    # Oldest statement first, so running balances and transfer pairing see the
    # months in order.
    collected.sort(key=lambda f: f[0].lower())
    batch = importer.stash_batch(collected, acct_id) if len(collected) > 1 else None
    if batch:
        return _preview_batch_item(request, conn, batch)

    filename, data = collected[0]
    try:
        stmt = load_statement(filename, data)
    except ValueError as e:
        return render(request, conn, "import_upload.html", accounts=_accounts(conn),
                      supported=", ".join(SUPPORTED_EXTENSIONS), error=str(e))
    stmt, saved_profile = _apply_saved_profile(conn, acct_id, filename, data, stmt)
    token = importer.stash_pending(filename, data, acct_id)
    return _render_preview(request, conn, token, filename, acct_id, stmt,
                           saved_profile)


def _apply_saved_profile(conn, acct_id: int, filename: str, data: bytes, stmt):
    """Reuse the column mapping confirmed for this bank's layout before."""
    if stmt.kind != "table":
        return stmt, False
    profile = importer.load_profile(conn, acct_id, stmt.header_sig)
    if profile is None:
        return stmt, False
    profile.header_row = stmt.mapping.header_row      # position is file-specific
    return load_statement(filename, data, mapping=profile), True


def _expired(request, conn, batch: str = ""):
    """A readable page rather than a raw error when an upload is no longer held."""
    state = importer.load_batch(batch) if batch else None
    remaining = 0
    if state:
        remaining = sum(1 for t in state["tokens"][state["index"]:]
                        if importer.load_pending(t) is not None)
    return render(request, conn, "import_expired.html",
                  batch=batch if remaining else "", remaining=remaining)


@router.get("/import/resume/{batch}")
def import_resume(batch: str, request: Request, conn=Depends(get_conn),
                  user=Depends(current_user)):
    """Pick a partly-finished batch back up at its next readable file."""
    if importer.load_batch(batch) is None:
        return _expired(request, conn)
    return _preview_batch_item(request, conn, batch)


def _preview_batch_item(request: Request, conn, batch: str):
    """Show the preview for the batch's current file, skipping unreadable ones."""
    state = importer.load_batch(batch)
    if state is None:
        return _expired(request, conn)
    acct_id = state["account_id"]
    while state["index"] < len(state["tokens"]):
        token = state["tokens"][state["index"]]
        pending = importer.load_pending(token)
        if pending is None:
            state["index"] += 1
            continue
        filename, data, _ = pending
        try:
            stmt = load_statement(filename, data)
        except ValueError as e:
            state["skipped"].append({"filename": filename, "reason": str(e)})
            state["index"] += 1
            importer.save_batch(batch, state)
            continue
        stmt, saved_profile = _apply_saved_profile(conn, acct_id, filename, data, stmt)
        importer.save_batch(batch, state)
        return _render_preview(request, conn, token, filename, acct_id, stmt,
                               saved_profile, batch=batch,
                               batch_pos=state["index"] + 1,
                               batch_total=len(state["tokens"]))
    return _finish_batch(request, conn, batch, state)


def _finish_batch(request: Request, conn, batch: str, state: dict):
    totals = {"added": 0, "duplicates": 0, "failed": 0, "categorized": 0,
              "to_confirm": 0}
    for entry in state["results"]:
        for key in totals:
            totals[key] += entry.get(key, 0)
    importer.drop_batch(batch)
    linked = transfers.auto_link(conn)
    uncat = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL").fetchone()[0]
    return render(request, conn, "import_batch_result.html",
                  totals=totals, results=state["results"],
                  skipped=state["skipped"], uncat=uncat, linked=linked,
                  transfer_suggestions=transfers.suggestion_count(conn),
                  to_confirm=splits.pending_confirmation_count(conn))


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
    batch = str(form.get("batch", ""))
    pending = importer.load_pending(token)
    if pending is None:
        # One file of a batch can go missing without losing the rest.
        if batch and importer.load_batch(batch) is not None:
            state = importer.load_batch(batch)
            if state["index"] < len(state["tokens"]) \
                    and state["tokens"][state["index"]] == token:
                state["skipped"].append({"filename": "a queued file",
                                         "reason": "no longer held"})
                state["index"] += 1
                importer.save_batch(batch, state)
            return _preview_batch_item(request, conn, batch)
        return _expired(request, conn, batch)
    filename, data, account_id = pending

    stmt = load_statement(filename, data)
    if stmt.kind == "table":
        mapping = _mapping_from_form(form)
        if mapping.amount_col is None and mapping.debit_col is None \
                and mapping.credit_col is None:
            raise HTTPException(status_code=400, detail="Pick an amount column.")
        stmt.mapping = mapping
        stmt.parsed = apply_mapping(stmt.rows, mapping)

    state = importer.load_batch(batch) if batch else None

    if form.get("action") == "refresh":
        return _render_preview(request, conn, token, filename, account_id, stmt,
                               saved_profile=False, batch=batch,
                               batch_pos=(state["index"] + 1) if state else 0,
                               batch_total=len(state["tokens"]) if state else 0)

    if form.get("action") == "skip" and state is not None:
        state["skipped"].append({"filename": filename, "reason": "skipped by you"})
        state["index"] += 1
        importer.save_batch(batch, state)
        importer.drop_pending(token)
        return _preview_batch_item(request, conn, batch)

    result = importer.commit_import(conn, account_id, filename, data, stmt.parsed,
                                    user["id"])
    if stmt.kind == "table":
        importer.save_profile(conn, account_id, stmt.header_sig, stmt.mapping)
    importer.drop_pending(token)

    if state is not None:
        state["results"].append({
            "filename": filename, "added": result.added,
            "duplicates": result.duplicates, "failed": result.failed,
            "categorized": result.categorized, "to_confirm": result.to_confirm})
        state["index"] += 1
        importer.save_batch(batch, state)
        return _preview_batch_item(request, conn, batch)

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
