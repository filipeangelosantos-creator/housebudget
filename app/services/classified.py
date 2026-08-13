"""Auditing the categories the app chose for you.

The review queue only shows what the classifier *couldn't* place. Everything it
placed confidently goes straight into the budget without ever being looked at,
and a single wrong guess about a frequent merchant quietly skews a whole
category for months.

This groups transactions by merchant so the whole history of one shop is a
single line you can read and change, instead of hundreds of identical rows. The
group, the transactions behind it and what a change re-files are all the same
set — the source filter decides which merchants are worth showing, never which
of a merchant's transactions count.
"""
from . import classify

# What decided the category. Kept short because it is shown as a chip.
SOURCE_LABEL = {
    "rule": "rule",
    "guess": "learned from you",
    "user": "your choice",
    "": "not filed yet",
}

AUTO_SOURCES = ("rule", "guess")


def _scope(month: str | None, account: int | None, q: str = ""):
    """Which transactions are in play at all. Deliberately not filtered by
    category: a merchant's uncategorized rows belong to the same merchant."""
    where: list[str] = ["1 = 1"]
    params: list = []
    if month:
        where.append("substr(t.date,1,7) = ?")
        params.append(month)
    if account:
        where.append("t.account_id = ?")
        params.append(account)
    if q and q.strip():
        where.append("(t.description LIKE ? OR t.merchant_key LIKE ?)")
        params.extend([f"%{q.strip()}%", f"%{q.strip().upper()}%"])
    return where, params


def _qualifies(group: dict, source: str) -> bool:
    filed = {s for s in group["sources"] if s}
    if not filed:
        return False           # nothing filed here yet; that is the review queue
    if source == "auto":
        return bool(filed & set(AUTO_SOURCES))
    if source == "user":
        return "user" in filed
    return True


def merchant_groups(conn, month: str | None = None, account: int | None = None,
                    source: str = "auto", q: str = "",
                    limit: int = 200) -> list[dict]:
    """One row per merchant: what it is filed as, how much it accounts for, and
    what decided that. Merchants filed under more than one category say so
    rather than being averaged into a single answer."""
    where, params = _scope(month, account, q)
    rows = conn.execute(
        f"""SELECT t.merchant_key, t.category_id, t.classified_by, t.rule_id,
                   c.name AS category_name, r.pattern AS rule_pattern,
                   COUNT(*) AS n, SUM(t.amount_cents) AS total,
                   MIN(t.date) AS first_date, MAX(t.date) AS last_date,
                   MIN(t.description) AS sample,
                   SUM(CASE WHEN EXISTS (SELECT 1 FROM transaction_splits s
                                         WHERE s.transaction_id = t.id)
                            THEN 1 ELSE 0 END) AS n_split
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            LEFT JOIN rules r ON r.id = t.rule_id
            WHERE {' AND '.join(where)}
            GROUP BY t.merchant_key, t.category_id, t.classified_by, t.rule_id""",
        params).fetchall()

    merged: dict[str, dict] = {}
    for r in rows:
        g = merged.setdefault(r["merchant_key"], {
            "merchant_key": r["merchant_key"], "sample": r["sample"],
            "n": 0, "total": 0, "n_split": 0, "n_unfiled": 0,
            "first_date": r["first_date"], "last_date": r["last_date"],
            "buckets": [], "sources": set(), "rules": set(),
            "orphan_rule": False,
        })
        g["n"] += r["n"]
        g["total"] += r["total"]
        g["n_split"] += r["n_split"]
        g["first_date"] = min(g["first_date"], r["first_date"])
        g["last_date"] = max(g["last_date"], r["last_date"])
        g["sources"].add(r["classified_by"])
        if r["category_id"] is None:
            g["n_unfiled"] += r["n"]
            continue
        if r["rule_pattern"]:
            g["rules"].add(r["rule_pattern"])
        elif r["classified_by"] == "rule":
            # The rule that filed these was deleted since; nothing owns them now.
            g["orphan_rule"] = True
        g["buckets"].append({"category_id": r["category_id"],
                             "category_name": r["category_name"], "n": r["n"]})

    out = []
    for g in merged.values():
        if not _qualifies(g, source):
            continue
        g["buckets"].sort(key=lambda b: -b["n"])
        main = g["buckets"][0]
        g["category_id"] = main["category_id"]
        g["category_name"] = main["category_name"]
        g["mixed"] = len({b["category_id"] for b in g["buckets"]}) > 1
        g["sources"] = sorted(s for s in g["sources"] if s)
        g["rules"] = sorted(g["rules"])
        g["why"] = _why(g)
        out.append(g)
    # Biggest spend first: that is where a wrong category costs you the most.
    out.sort(key=lambda g: (-abs(g["total"]), -g["n"]))
    return out[:limit]


def _why(group: dict) -> str:
    if group["orphan_rule"]:
        return "filed by a rule that no longer exists"
    if group["rules"]:
        joined = ", ".join(f"“{p}”" for p in group["rules"][:2])
        more = f" +{len(group['rules']) - 2} more" if len(group["rules"]) > 2 else ""
        return f"rule {joined}{more}"
    if group["sources"] == ["guess"]:
        return "copied from how you filed this shop before"
    if group["sources"] == ["user"]:
        return "you chose this"
    return " + ".join(SOURCE_LABEL.get(s, s) for s in group["sources"])


def group_transactions(conn, merchant_key: str, month: str | None = None,
                       account: int | None = None, limit: int = 200) -> list[dict]:
    """The individual transactions behind one merchant row."""
    where, params = _scope(month, account)
    where.append("t.merchant_key = ?")
    params.append(merchant_key)
    rows = conn.execute(
        f"""SELECT t.id, t.date, t.description, t.amount_cents,
                   a.name AS account_name, c.name AS category_name
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE {' AND '.join(where)}
            ORDER BY t.date DESC LIMIT ?""", params + [limit]).fetchall()
    return [dict(r) for r in rows]


def remember_merchant(conn, merchant_key: str, category_id: int) -> int:
    """Make this merchant's category stick for future imports.

    An exact match on the merchant key, so it can only ever catch this shop —
    a `contains` pattern built from a merchant name is what makes rules bleed
    into unrelated transactions.
    """
    existing = conn.execute(
        "SELECT id FROM rules WHERE pattern = ? AND match_type = 'exact'",
        (merchant_key,)).fetchone()
    if existing:
        conn.execute("UPDATE rules SET category_id = ? WHERE id = ?",
                     (category_id, existing["id"]))
        return existing["id"]
    return classify.create_rule(conn, merchant_key, category_id,
                                match_type="exact", priority=5)


def refile(conn, merchant_key: str, category_id: int, remember: bool = True,
           month: str | None = None, account: int | None = None) -> int:
    """Re-file every transaction of one merchant. Returns how many changed."""
    where, params = _scope(month, account)
    where.append("t.merchant_key = ?")
    params.append(merchant_key)
    ids = [r["id"] for r in conn.execute(
        f"SELECT t.id FROM transactions t WHERE {' AND '.join(where)}",
        params).fetchall()]
    if not ids:
        return 0

    rule_id = remember_merchant(conn, merchant_key, category_id) if remember else None
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        # One category replaces the whole allocation, so any split it had is gone.
        conn.execute(
            f"DELETE FROM transaction_splits WHERE transaction_id IN ({marks})", chunk)
        conn.execute(
            f"UPDATE transactions SET category_id = ?, needs_review = 0, "
            f"classified_by = ?, rule_id = ? WHERE id IN ({marks})",
            [category_id, "rule" if rule_id else "user", rule_id] + chunk)
    conn.commit()
    return len(ids)


def auto_filed_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE classified_by IN ('rule','guess')"
    ).fetchone()[0]
