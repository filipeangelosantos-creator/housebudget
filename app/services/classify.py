"""Expense classification: description normalization, rules engine, learning.

Rules match against the normalized (uppercased) description or the merchant
key. Lower priority number wins; ties broken by longer (more specific)
pattern. When a user corrects a category and asks to remember it, a 'learned'
rule (priority 10) is created so it beats seed rules (priority ~100).
"""
import re

from ..db import utcnow

_WS = re.compile(r"\s+")
_HAS_DIGIT = re.compile(r"\d")
_PUNCT = re.compile(r"[^\w&/.\-]")

# Tokens that carry no merchant information
NOISE_TOKENS = {
    "POS", "DEBIT", "CREDIT", "PURCHASE", "CARD", "VISA", "MASTERCARD",
    "INTERAC", "ACH", "PPD", "WEB", "TFR", "AUTH", "PENDING", "RECURRING",
    "PREAUTH", "PRE-AUTH", "COMPRA", "PAGAMENTO", "DD", "SEPA", "BILL",
}


def normalize_desc(desc: str) -> str:
    return _WS.sub(" ", str(desc or "")).strip().upper()


def merchant_key(desc: str) -> str:
    """Stable key for grouping the same merchant across transactions.

    Card processors glue a reference onto the merchant name in one token —
    ONEQUINCE*Q28484714, TJMAXX#0569 — so tokens are split on those joiners
    *before* digit-bearing parts are dropped. Doing it the other way around
    discarded the merchant and left only the city as the key.
    """
    tokens = []
    for tok in normalize_desc(desc).split(" "):
        for piece in re.split(r"[*#]", tok):
            if not piece or _HAS_DIGIT.search(piece):
                continue
            cleaned = _PUNCT.sub("", piece).strip()
            if cleaned and cleaned not in NOISE_TOKENS and len(cleaned) > 1:
                tokens.append(cleaned)
    return " ".join(tokens[:6]) if tokens else normalize_desc(desc)[:40]


# Processor/platform prefixes that appear before the actual merchant name.
# A rule on the prefix alone would match half the card statement.
AGGREGATOR_PREFIXES = {
    "PAYPAL", "GOOGLE", "AMZN", "AMAZON", "APPLE.COM", "SQ", "SP", "TST",
    "MED", "IC", "PP", "CKE", "DD", "EB", "ZSK", "CLOVER", "STRIPE", "WIX",
    "SHOPIFY", "FSP", "PY", "LSK", "MKTPL",
}

# Words too common to stand alone in a rule: "AUTO" would file your car
# insurance under whatever you once picked for an automatic payment.
GENERIC_WORDS = {
    "AUTO", "AUTOMATIC", "POINT", "PAYMENT", "PAYMENTS", "PAY", "ONLINE",
    "MOBILE", "ELECTRONIC", "RECURRING", "MONTHLY", "ANNUAL", "PURCHASE",
    "STORE", "SHOP", "MARKET", "CENTER", "CENTRE", "SERVICE", "SERVICES",
    "COMPANY", "GROUP", "NATIONAL", "AMERICAN", "UNITED", "GENERAL", "FIRST",
    "ONE", "THE", "AND", "NEW", "CITY", "TOWN", "STATE", "COUNTY", "TOTAL",
    "DIRECT", "EXPRESS", "PRIME", "SUPER", "GRAND", "ROYAL", "GLOBAL",
    "INTERNATIONAL", "LLC", "INC", "LTD", "CORP", "CO", "PLC", "SA", "LDA",
    "TRANSFER", "DEPOSIT", "WITHDRAWAL", "CHARGE", "CREDIT", "DEBIT", "BANK",
}

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

MAX_PATTERN_TOKENS = 3


def _too_weak(token: str) -> bool:
    return (len(token) < 5 or token in GENERIC_WORDS
            or token in AGGREGATOR_PREFIXES)


def suggest_pattern(desc: str) -> str:
    """A rule pattern specific enough to be safe to apply automatically.

    Grows from one token until it ends on something distinctive, so
    "POINT AND BALANCE LLC NEEDHAM MA" suggests "POINT AND BALANCE" rather
    than "POINT", which would also catch every other merchant with that word.
    """
    tokens = [t for t in merchant_key(desc).split(" ") if t]
    if len(tokens) >= 2 and tokens[-1] in US_STATES:
        tokens = tokens[:-1]                     # trailing state code is noise
    if not tokens:
        return merchant_key(desc)

    take = 1
    while take < min(len(tokens), MAX_PATTERN_TOKENS) and _too_weak(tokens[take - 1]):
        take += 1
    return " ".join(tokens[:take])


def load_rules(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT r.id, r.pattern, r.match_type, r.category_id, r.priority "
        "FROM rules r JOIN categories c ON c.id = r.category_id "
        "WHERE c.archived = 0 "
        "ORDER BY r.priority ASC, LENGTH(r.pattern) DESC, r.id ASC"
    ).fetchall()
    out = []
    for r in rows:
        rule = dict(r)
        rule["pattern_up"] = rule["pattern"].upper()
        if rule["match_type"] == "regex":
            try:
                rule["compiled"] = re.compile(rule["pattern"], re.IGNORECASE)
            except re.error:
                continue
        out.append(rule)
    return out


def rule_matches(rule: dict, normalized: str, mkey: str) -> bool:
    if rule["match_type"] == "contains":
        return rule["pattern_up"] in normalized or rule["pattern_up"] in mkey
    if rule["match_type"] == "exact":
        return rule["pattern_up"] == mkey or rule["pattern_up"] == normalized
    if rule["match_type"] == "regex":
        return bool(rule["compiled"].search(normalized))
    return False


def matching_rule(rules: list[dict], normalized: str, mkey: str) -> dict | None:
    """The rule that decides this description, or None."""
    for rule in rules:
        if rule_matches(rule, normalized, mkey):
            return rule
    return None


def classify(rules: list[dict], normalized: str, mkey: str) -> int | None:
    rule = matching_rule(rules, normalized, mkey)
    return rule["category_id"] if rule else None


def create_rule(conn, pattern: str, category_id: int, match_type: str = "contains",
                priority: int = 10, source: str = "learned") -> int:
    """Teach a rule, or point an identical one at the new category.

    Ticking "always file this here" used to add a rule every time, so filing
    the same shop twice left two identical rules and the second could never
    fire. Same pattern, same match type, same origin means it is the same rule
    — changing your mind about the category rewrites it.
    """
    pattern = pattern.strip()
    existing = conn.execute(
        "SELECT id FROM rules WHERE pattern = ? COLLATE NOCASE AND match_type = ? "
        "AND source = ? ORDER BY priority, id LIMIT 1",
        (pattern, match_type, source)).fetchone()
    if existing:
        conn.execute("UPDATE rules SET category_id = ?, pattern = ? WHERE id = ?",
                     (category_id, pattern, existing["id"]))
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO rules (pattern, match_type, category_id, priority, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pattern, match_type, category_id, priority, source, utcnow()),
    )
    return cur.lastrowid


def duplicate_groups(conn) -> list[dict]:
    """Rules that share a pattern and match type, so only the first can fire.

    Ordering mirrors load_rules exactly — otherwise this would name a survivor
    the engine doesn't actually use.
    """
    rows = conn.execute(
        "SELECT r.id, r.pattern, r.match_type, r.category_id, r.priority, r.source, "
        "       c.name AS category_name "
        "FROM rules r JOIN categories c ON c.id = r.category_id "
        "ORDER BY r.priority ASC, LENGTH(r.pattern) DESC, r.id ASC").fetchall()
    grouped: dict[tuple, list] = {}
    for r in rows:
        grouped.setdefault((r["pattern"].upper(), r["match_type"]), []).append(dict(r))

    groups = []
    for (pattern, match_type), members in grouped.items():
        if len(members) < 2:
            continue
        keep, drop = members[0], members[1:]
        # Judged per rule, not per group: one set can hold both an exact copy
        # and a rule naming another category, and only the first is pure noise.
        redundant = [d for d in drop if d["category_id"] == keep["category_id"]]
        conflicting = [d for d in drop if d["category_id"] != keep["category_id"]]
        groups.append({
            "pattern": pattern, "match_type": match_type,
            "keep": keep, "drop": drop,
            "redundant": redundant, "conflicting_rules": conflicting,
            "conflicting": bool(conflicting),
        })
    groups.sort(key=lambda g: (not g["conflicting"], g["pattern"]))
    return groups


def _delete_rules(conn, ids: list[int], survivor_id: int) -> None:
    """Drop rules, moving anything they filed onto the rule that stays.

    Without this the transactions keep their category but lose the link, and
    show up as filed by a rule that no longer exists.
    """
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    conn.execute(f"UPDATE transactions SET rule_id = ? WHERE rule_id IN ({marks})",
                 [survivor_id] + ids)
    conn.execute(f"DELETE FROM rules WHERE id IN ({marks})", ids)


def remove_duplicate_rules(conn, include_conflicting: bool = False) -> int:
    """Delete rules that can never fire because an identical one precedes them.

    Behaviour is unchanged either way — these rules are already unreachable.
    Conflicting ones are left alone by default because each is a category
    somebody once chose, and that is worth seeing before it disappears.
    """
    removed = 0
    for group in duplicate_groups(conn):
        doomed = list(group["redundant"])
        if include_conflicting:
            doomed += group["conflicting_rules"]
        ids = [d["id"] for d in doomed]
        _delete_rules(conn, ids, group["keep"]["id"])
        removed += len(ids)
    conn.commit()
    return removed


def keep_only(conn, rule_id: int) -> int:
    """Within one duplicate group, keep this rule and drop the rest."""
    for group in duplicate_groups(conn):
        ids = [m["id"] for m in [group["keep"], *group["drop"]]]
        if rule_id not in ids:
            continue
        losers = [i for i in ids if i != rule_id]
        _delete_rules(conn, losers, rule_id)
        conn.commit()
        return len(losers)
    return 0


def rule_usage(conn) -> dict[int, int]:
    """How many transactions each rule currently accounts for, in one pass."""
    return {r["rule_id"]: r["n"] for r in conn.execute(
        "SELECT rule_id, COUNT(*) AS n FROM transactions "
        "WHERE rule_id IS NOT NULL GROUP BY rule_id")}


def apply_rules_to_uncategorized(conn, only_rule_id: int | None = None) -> int:
    """Run rules over uncategorized transactions; returns number categorized."""
    rules = load_rules(conn)
    if only_rule_id is not None:
        rules = [r for r in rules if r["id"] == only_rule_id]
    if not rules:
        return 0
    updated = 0
    rows = conn.execute(
        "SELECT id, normalized_desc, merchant_key FROM transactions "
        "WHERE category_id IS NULL"
    ).fetchall()
    for row in rows:
        rule = matching_rule(rules, row["normalized_desc"], row["merchant_key"])
        if rule is not None:
            conn.execute(
                "UPDATE transactions SET category_id = ?, classified_by = 'rule', "
                "rule_id = ? WHERE id = ?", (rule["category_id"], rule["id"], row["id"]))
            updated += 1
    conn.commit()
    return updated


def label_existing_classifications(conn) -> int:
    """One-off labelling for rows filed before we recorded who filed them.

    A row whose category is what a rule produces today is attributed to that
    rule; anything else is treated as your own choice, which is the safe way
    round — the audit list then never hides a category you set by hand.
    """
    rules = load_rules(conn)
    labelled = 0
    for row in conn.execute(
            "SELECT id, normalized_desc, merchant_key, category_id FROM transactions "
            "WHERE category_id IS NOT NULL AND classified_by = ''").fetchall():
        rule = matching_rule(rules, row["normalized_desc"], row["merchant_key"])
        if rule is not None and rule["category_id"] == row["category_id"]:
            conn.execute("UPDATE transactions SET classified_by = 'rule', rule_id = ? "
                         "WHERE id = ?", (rule["id"], row["id"]))
        else:
            conn.execute("UPDATE transactions SET classified_by = 'user' WHERE id = ?",
                         (row["id"],))
        labelled += 1
    conn.commit()
    return labelled


def count_rule_matches(conn, pattern: str, match_type: str) -> int:
    """How many existing transactions would this rule match (for rule preview)."""
    rule = {"pattern": pattern, "pattern_up": pattern.upper(),
            "match_type": match_type}
    if match_type == "regex":
        try:
            rule["compiled"] = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return 0
    n = 0
    for row in conn.execute(
            "SELECT normalized_desc, merchant_key FROM transactions").fetchall():
        if rule_matches(rule, row["normalized_desc"], row["merchant_key"]):
            n += 1
    return n
