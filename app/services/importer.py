"""Import pipeline: preview stats, dedupe and commit of parsed statements.

Dedupe strategy: OFX FITIDs are used when present; otherwise a hash of
(account, date, amount, normalized description, occurrence-index-within-file).
The occurrence index lets two identical real purchases in one file both import,
while re-importing an overlapping statement skips existing rows.
"""
import hashlib
import json
import secrets
from dataclasses import dataclass

from .. import config
from ..db import utcnow
from ..parsing.csv_parser import Mapping, ParsedRow
from . import classify, splits


def dedupe_hash_for(account_id: int, date: str, amount_cents: int,
                    normalized_desc: str, seq: int = 0,
                    fitid: str | None = None) -> str:
    """The identity a transaction has within an account. Shared with account
    merging so a merged-in transaction is still recognised as a duplicate the
    next time that statement is imported."""
    if fitid:
        src = f"{account_id}|fitid|{fitid}"
    else:
        src = f"{account_id}|{date}|{amount_cents}|{normalized_desc}|{seq}"
    return hashlib.sha256(src.encode()).hexdigest()


def dedupe_hashes(account_id: int, rows: list[ParsedRow]) -> list[str | None]:
    seen: dict[tuple, int] = {}
    hashes: list[str | None] = []
    for row in rows:
        if row.error:
            hashes.append(None)
            continue
        if row.fitid:
            src = f"{account_id}|fitid|{row.fitid}"
        else:
            norm = classify.normalize_desc(row.description)
            key = (str(row.date), row.amount_cents, norm)
            seq = seen.get(key, 0)
            seen[key] = seq + 1
            src = f"{account_id}|{row.date}|{row.amount_cents}|{norm}|{seq}"
        hashes.append(hashlib.sha256(src.encode()).hexdigest())
    return hashes


def find_existing(conn, hashes: list[str | None]) -> set[str]:
    existing: set[str] = set()
    valid = [h for h in hashes if h]
    for i in range(0, len(valid), 500):
        chunk = valid[i:i + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT dedupe_hash FROM transactions WHERE dedupe_hash IN ({marks})",
            chunk).fetchall()
        existing.update(r["dedupe_hash"] for r in rows)
    return existing


@dataclass
class PreviewStats:
    total: int
    ok: int
    failed: int
    duplicates: int
    would_categorize: int
    date_min: str
    date_max: str


def preview_stats(conn, account_id: int, rows: list[ParsedRow]) -> PreviewStats:
    hashes = dedupe_hashes(account_id, rows)
    existing = find_existing(conn, hashes)
    rules = classify.load_rules(conn)
    ok = [r for r in rows if not r.error]
    dup = sum(1 for r, h in zip(rows, hashes) if not r.error and h in existing)
    categorized = 0
    for r in ok:
        norm = classify.normalize_desc(r.description)
        if classify.classify(rules, norm, classify.merchant_key(r.description)):
            categorized += 1
    dates = sorted(str(r.date) for r in ok)
    return PreviewStats(
        total=len(rows), ok=len(ok), failed=len(rows) - len(ok), duplicates=dup,
        would_categorize=categorized,
        date_min=dates[0] if dates else "", date_max=dates[-1] if dates else "")


@dataclass
class ImportResult:
    import_id: int
    added: int
    duplicates: int
    failed: int
    categorized: int
    to_confirm: int = 0


def commit_import(conn, account_id: int, filename: str, file_bytes: bytes,
                  rows: list[ParsedRow], user_id: int) -> ImportResult:
    hashes = dedupe_hashes(account_id, rows)
    existing = find_existing(conn, hashes)
    rules = classify.load_rules(conn)
    now = utcnow()

    cur = conn.execute(
        "INSERT INTO imports (account_id, filename, file_sha256, uploaded_by, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_id, filename, hashlib.sha256(file_bytes).hexdigest(), user_id, now))
    import_id = cur.lastrowid

    added = dup = failed = categorized = to_confirm = 0
    for row, h in zip(rows, hashes):
        if row.error or h is None:
            failed += 1
            continue
        if h in existing:
            dup += 1
            continue
        norm = classify.normalize_desc(row.description)
        mkey = classify.merchant_key(row.description)
        cat = classify.classify(rules, norm, mkey)
        # Big-box / marketplace charges get a guess plus a request to confirm,
        # because one receipt there often spans several budget categories.
        prompt = splits.should_prompt_split(conn, norm, mkey, row.amount_cents)
        if cat is None and prompt:
            suggestion = splits.suggest_category(conn, mkey)
            if suggestion:
                cat = suggestion["category_id"]
        conn.execute(
            "INSERT INTO transactions (account_id, import_id, date, amount_cents, "
            "description, normalized_desc, merchant_key, category_id, fitid, "
            "dedupe_hash, needs_review, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, import_id, str(row.date), row.amount_cents,
             str(row.description)[:300], norm[:300], mkey, cat, row.fitid, h,
             1 if (prompt and cat is not None) else 0, now))
        existing.add(h)
        added += 1
        if cat:
            categorized += 1
            if prompt:
                to_confirm += 1

    conn.execute(
        "UPDATE imports SET num_added = ?, num_duplicate = ?, num_failed = ? WHERE id = ?",
        (added, dup, failed, import_id))
    conn.commit()

    config.ensure_dirs()
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-")[:80]
    (config.UPLOADS_DIR / f"{import_id}_{safe_name}").write_bytes(file_bytes)
    return ImportResult(import_id, added, dup, failed, categorized, to_confirm)


def delete_import(conn, import_id: int) -> int:
    """Remove an import and all transactions it created (fixes bad-mapping mistakes)."""
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE import_id = ?",
                     (import_id,)).fetchone()[0]
    conn.execute("DELETE FROM transactions WHERE import_id = ?", (import_id,))
    conn.execute("DELETE FROM imports WHERE id = ?", (import_id,))
    conn.commit()
    for f in config.UPLOADS_DIR.glob(f"{import_id}_*"):
        f.unlink(missing_ok=True)
    return n


def save_profile(conn, account_id: int, header_sig: str, mapping: Mapping) -> None:
    conn.execute(
        "INSERT INTO import_profiles (account_id, header_sig, config_json, created_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(account_id, header_sig) "
        "DO UPDATE SET config_json = excluded.config_json",
        (account_id, header_sig, json.dumps(mapping.to_dict()), utcnow()))
    conn.commit()


def load_profile(conn, account_id: int, header_sig: str) -> Mapping | None:
    row = conn.execute(
        "SELECT config_json FROM import_profiles WHERE account_id = ? AND header_sig = ?",
        (account_id, header_sig)).fetchone()
    if not row:
        return None
    try:
        return Mapping.from_dict(json.loads(row["config_json"]))
    except (ValueError, TypeError):
        return None


# --- pending uploads (between preview and confirm) ---------------------------

def stash_pending(filename: str, data: bytes, account_id: int) -> str:
    config.ensure_dirs()
    token = secrets.token_hex(16)
    (config.PENDING_DIR / f"{token}.bin").write_bytes(data)
    # utf-8 explicitly: statement filenames carry accents, and Windows would
    # otherwise use the local ANSI codepage and fail on them.
    (config.PENDING_DIR / f"{token}.json").write_text(
        json.dumps({"filename": filename, "account_id": account_id}),
        encoding="utf-8")
    _cleanup_pending()
    return token


def load_pending(token: str) -> tuple[str, bytes, int] | None:
    if not token.isalnum():
        return None
    bin_path = config.PENDING_DIR / f"{token}.bin"
    meta_path = config.PENDING_DIR / f"{token}.json"
    if not bin_path.exists() or not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return meta["filename"], bin_path.read_bytes(), int(meta["account_id"])


def drop_pending(token: str) -> None:
    (config.PENDING_DIR / f"{token}.bin").unlink(missing_ok=True)
    (config.PENDING_DIR / f"{token}.json").unlink(missing_ok=True)


# --- batches: several statements queued for one account ----------------------

def stash_batch(files: list[tuple[str, bytes]], account_id: int) -> str:
    """Queue several uploads; each is still previewed and confirmed in turn."""
    config.ensure_dirs()
    tokens = [stash_pending(name, data, account_id) for name, data in files]
    batch = secrets.token_hex(16)
    (config.PENDING_DIR / f"b{batch}.json").write_text(json.dumps({
        "account_id": account_id, "tokens": tokens, "index": 0,
        "names": [name for name, _ in files], "results": [], "skipped": []}),
        encoding="utf-8")
    return batch


def load_batch(batch: str) -> dict | None:
    if not batch.isalnum():
        return None
    path = config.PENDING_DIR / f"b{batch}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_batch(batch: str, state: dict) -> None:
    (config.PENDING_DIR / f"b{batch}.json").write_text(
        json.dumps(state), encoding="utf-8")


def drop_batch(batch: str) -> None:
    state = load_batch(batch)
    if state:
        for token in state.get("tokens", []):
            drop_pending(token)
    (config.PENDING_DIR / f"b{batch}.json").unlink(missing_ok=True)


def _cleanup_pending(max_files: int = 40) -> None:
    files = sorted(config.PENDING_DIR.glob("*"), key=lambda p: p.stat().st_mtime)
    for f in files[:-max_files]:
        f.unlink(missing_ok=True)
