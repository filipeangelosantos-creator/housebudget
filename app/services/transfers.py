"""Pairing the two sides of an internal money movement.

Moving money between your own accounts — savings to checking, checking to a
credit card — shows up twice: once leaving one account, once arriving in the
other. Neither is income or spending, so both sides must be kept out of the
budget. Matching on description text alone breaks whenever a bank words things
differently, so this module matches the two sides to *each other*: same amount,
opposite sign, different accounts, a few days apart.

Obvious pairs (an amount match plus transfer-ish wording) link automatically.
Anything less certain is offered as a suggestion for you to confirm, because a
false pair silently hides real spending.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from ..db import utcnow

# Wording that makes a same-amount match near-certain rather than coincidental.
TRANSFER_HINTS = [
    "TRANSFER", "TRANSFERENCIA", "TRANSFERENCE", "XFER", "TFR", "E-TFR",
    "PAYMENT THANK", "PAYMENT - THANK", "PAYMENT RECEIVED", "AUTOPAY",
    "BILL PAY", "BILLPAY", "ONLINE BANKING", "ONLINE PMT", "PAGAMENTO",
    "MB WAY", "ZELLE", "VENMO", "INTERAC", "WITHDRAWAL TO", "DEPOSIT FROM",
    "TO SAVINGS", "FROM SAVINGS", "TO CHECKING", "FROM CHECKING",
]

MAX_DAYS_AUTO = 3        # link automatically only within this window
MAX_DAYS_SUGGEST = 6     # look this far apart when suggesting
MIN_SUGGEST_CENTS = 1000  # below this, equal amounts are usually coincidence


@dataclass
class Candidate:
    out_txn: dict
    in_txn: dict
    days_apart: int
    has_hint: bool

    @property
    def amount_cents(self) -> int:
        return abs(self.out_txn["amount_cents"])

    @property
    def auto(self) -> bool:
        """Confident enough to link without asking."""
        return self.has_hint and self.days_apart <= MAX_DAYS_AUTO

    @property
    def why(self) -> str:
        when = ("same day" if self.days_apart == 0
                else f"{self.days_apart} day{'s' if self.days_apart != 1 else ''} apart")
        return f"same amount, {when}" + (", transfer wording" if self.has_hint else "")

    @property
    def group_key(self) -> str:
        """Recurring moves (the same pair of descriptions every month) share
        this, so they can be confirmed in one go instead of one by one."""
        return f"{self.out_txn['merchant_key']}→{self.in_txn['merchant_key']}"


def _has_hint(*texts: str) -> bool:
    blob = " ".join(t or "" for t in texts).upper()
    return any(h in blob for h in TRANSFER_HINTS)


def _parse(d: str) -> date:
    return date(int(d[0:4]), int(d[5:7]), int(d[8:10]))


def find_candidates(conn, max_days: int = MAX_DAYS_SUGGEST) -> list[Candidate]:
    """Unpaired, undismissed transactions that look like two sides of one move."""
    rows = conn.execute(
        """SELECT t.id, t.account_id, t.date, t.amount_cents, t.description,
                  t.normalized_desc, t.merchant_key, a.name AS account_name,
                  a.type AS account_type
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE NOT EXISTS (SELECT 1 FROM transfer_links l
                             WHERE l.out_txn_id = t.id OR l.in_txn_id = t.id)
           ORDER BY t.date, t.id""").fetchall()
    dismissed = {(r["out_txn_id"], r["in_txn_id"]) for r in
                 conn.execute("SELECT out_txn_id, in_txn_id FROM transfer_dismissals")}

    incoming: dict[int, list[dict]] = {}
    for r in rows:
        if r["amount_cents"] > 0:
            incoming.setdefault(r["amount_cents"], []).append(dict(r))

    used_in: set[int] = set()
    candidates: list[Candidate] = []
    # Outgoing rows are consumed in date order so the closest match wins first.
    for r in rows:
        if r["amount_cents"] >= 0:
            continue
        amount = -r["amount_cents"]
        if amount < MIN_SUGGEST_CENTS:
            continue
        out = dict(r)
        out_date = _parse(out["date"])
        best: tuple[int, dict] | None = None
        for cand in incoming.get(amount, []):
            if cand["id"] in used_in or cand["account_id"] == out["account_id"]:
                continue
            if (out["id"], cand["id"]) in dismissed:
                continue
            days = abs((_parse(cand["date"]) - out_date).days)
            if days > max_days:
                continue
            if best is None or days < best[0]:
                best = (days, cand)
        if best is None:
            continue
        days, cand = best
        used_in.add(cand["id"])
        candidates.append(Candidate(
            out_txn=out, in_txn=cand, days_apart=days,
            has_hint=_has_hint(out["normalized_desc"], cand["normalized_desc"])))
    return candidates


MANUAL_MAX_DAYS = 21


def manual_candidates(conn, txn_id: int, max_days: int = MANUAL_MAX_DAYS,
                      limit: int = 20) -> list[dict]:
    """Transactions that could be the other side of this one.

    Deliberately looser than the automatic matcher: any opposite-signed,
    unpaired transaction in a different account within a few weeks. Amounts
    need not match — a wire fee or an FX difference makes the two sides differ,
    and only you can say they belong together.
    """
    txn = conn.execute(
        "SELECT id, account_id, date, amount_cents FROM transactions WHERE id = ?",
        (txn_id,)).fetchone()
    if txn is None:
        return []
    rows = conn.execute(
        """SELECT t.id, t.date, t.description, t.amount_cents,
                  a.name AS account_name,
                  ABS(julianday(t.date) - julianday(?)) AS days_apart
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.account_id != ?
             AND ((? < 0 AND t.amount_cents > 0) OR (? > 0 AND t.amount_cents < 0))
             AND ABS(julianday(t.date) - julianday(?)) <= ?
             AND NOT EXISTS (SELECT 1 FROM transfer_links l
                             WHERE l.out_txn_id = t.id OR l.in_txn_id = t.id)
           ORDER BY ABS(ABS(t.amount_cents) - ?), days_apart
           LIMIT ?""",
        (txn["date"], txn["account_id"], txn["amount_cents"], txn["amount_cents"],
         txn["date"], max_days, abs(txn["amount_cents"]), limit)).fetchall()
    out = []
    for r in rows:
        entry = dict(r)
        entry["days_apart"] = int(entry["days_apart"])
        entry["exact_amount"] = abs(r["amount_cents"]) == abs(txn["amount_cents"])
        out.append(entry)
    return out


def unpaired(conn, q: str = "", account: int | None = None,
             limit: int = 60) -> list[dict]:
    """Transactions not yet part of a pair — the pool to pick a side from.

    The automatic matcher only offers pairs it spotted itself; this is what you
    search when you know two rows belong together and it didn't notice.
    """
    where = ["NOT EXISTS (SELECT 1 FROM transfer_links l "
             "WHERE l.out_txn_id = t.id OR l.in_txn_id = t.id)"]
    params: list = []
    if q.strip():
        where.append("(t.description LIKE ? OR t.normalized_desc LIKE ?)")
        params.extend([f"%{q.strip()}%", f"%{q.strip().upper()}%"])
    if account:
        where.append("t.account_id = ?")
        params.append(account)
    rows = conn.execute(
        f"""SELECT t.id, t.date, t.description, t.amount_cents,
                   a.name AS account_name
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE {' AND '.join(where)}
            ORDER BY t.date DESC, t.id DESC LIMIT ?""", params + [limit]).fetchall()
    return [dict(r) for r in rows]


def link_pair(conn, txn_a: int, txn_b: int, source: str = "manual") -> bool:
    """Link two transactions whichever way round they were given."""
    rows = {r["id"]: r["amount_cents"] for r in conn.execute(
        "SELECT id, amount_cents FROM transactions WHERE id IN (?, ?)",
        (txn_a, txn_b)).fetchall()}
    if len(rows) != 2 or txn_a == txn_b:
        return False
    # One side has to leave and the other arrive, or it isn't a movement.
    if (rows[txn_a] < 0) == (rows[txn_b] < 0):
        return False
    if rows[txn_a] < 0:
        return link(conn, txn_a, txn_b, source)
    return link(conn, txn_b, txn_a, source)


def linked_partner(conn, txn_id: int) -> dict | None:
    """The other side of this transaction, if it is part of a pair."""
    row = conn.execute(
        """SELECT l.id AS link_id, l.source,
                  t.id, t.date, t.description, t.amount_cents,
                  a.name AS account_name
           FROM transfer_links l
           JOIN transactions t ON t.id = CASE WHEN l.out_txn_id = ?
                                              THEN l.in_txn_id ELSE l.out_txn_id END
           JOIN accounts a ON a.id = t.account_id
           WHERE l.out_txn_id = ? OR l.in_txn_id = ?""",
        (txn_id, txn_id, txn_id)).fetchone()
    return dict(row) if row else None


def link(conn, out_txn_id: int, in_txn_id: int, source: str = "manual") -> bool:
    """Record a pair. Returns False when either side is already linked."""
    try:
        conn.execute(
            "INSERT INTO transfer_links (out_txn_id, in_txn_id, source, created_at) "
            "VALUES (?, ?, ?, ?)", (out_txn_id, in_txn_id, source, utcnow()))
    except Exception:
        return False
    # A confirmed transfer never needs category confirmation.
    conn.execute("UPDATE transactions SET needs_review = 0 WHERE id IN (?, ?)",
                 (out_txn_id, in_txn_id))
    conn.commit()
    return True


def unlink(conn, link_id: int) -> None:
    conn.execute("DELETE FROM transfer_links WHERE id = ?", (link_id,))
    conn.commit()


def dismiss(conn, out_txn_id: int, in_txn_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO transfer_dismissals (out_txn_id, in_txn_id) VALUES (?, ?)",
        (out_txn_id, in_txn_id))
    conn.commit()


def auto_link(conn) -> int:
    """Link every confident pair. Returns how many were linked."""
    linked = 0
    for c in find_candidates(conn, max_days=MAX_DAYS_AUTO):
        if c.auto and link(conn, c.out_txn["id"], c.in_txn["id"], source="auto"):
            linked += 1
    return linked


def suggestions(conn, limit: int = 25) -> list[Candidate]:
    """Pairs worth a human decision (the confident ones are already linked)."""
    return [c for c in find_candidates(conn) if not c.auto][:limit]


def group_sizes(conn) -> dict[str, int]:
    """How many pending suggestions share each recurring-move signature."""
    sizes: dict[str, int] = {}
    for c in suggestions(conn, limit=1000):
        sizes[c.group_key] = sizes.get(c.group_key, 0) + 1
    return sizes


def link_group(conn, group_key: str) -> int:
    """Link every pending suggestion matching one recurring move."""
    linked = 0
    for c in suggestions(conn, limit=1000):
        if c.group_key == group_key and link(conn, c.out_txn["id"], c.in_txn["id"]):
            linked += 1
    return linked


def dismiss_group(conn, group_key: str) -> int:
    dismissed = 0
    for c in suggestions(conn, limit=1000):
        if c.group_key == group_key:
            dismiss(conn, c.out_txn["id"], c.in_txn["id"])
            dismissed += 1
    return dismissed


def suggestion_count(conn) -> int:
    return len(suggestions(conn, limit=1000))


def linked_pairs(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """SELECT l.id, l.source, l.created_at,
                  o.id AS out_id, o.date AS out_date, o.description AS out_desc,
                  o.amount_cents AS amount_cents, ao.name AS out_account,
                  i.id AS in_id, i.date AS in_date, i.description AS in_desc,
                  ai.name AS in_account
           FROM transfer_links l
           JOIN transactions o ON o.id = l.out_txn_id
           JOIN transactions i ON i.id = l.in_txn_id
           JOIN accounts ao ON ao.id = o.account_id
           JOIN accounts ai ON ai.id = i.account_id
           ORDER BY o.date DESC, l.id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def linked_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM transfer_links").fetchone()[0]


def unmatched_transfers(conn, month: str) -> list[dict]:
    """Transactions filed as transfers/card payments whose other side was never
    seen. Usually means a statement is missing — and that hides real spending."""
    rows = conn.execute(
        """SELECT t.id, t.date, t.description, t.amount_cents, c.name AS category,
                  a.name AS account_name
           FROM transactions t
           JOIN categories c ON c.id = t.category_id
           JOIN accounts a ON a.id = t.account_id
           WHERE c.excluded = 1 AND substr(t.date,1,7) = ?
             AND NOT EXISTS (SELECT 1 FROM transfer_links l
                             WHERE l.out_txn_id = t.id OR l.in_txn_id = t.id)
           ORDER BY ABS(t.amount_cents) DESC""", (month,)).fetchall()
    return [dict(r) for r in rows]
