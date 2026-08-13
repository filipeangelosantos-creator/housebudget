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


def suggest_pattern(desc: str) -> str:
    """Suggested rule pattern when learning from a user correction."""
    key = merchant_key(desc)
    tokens = key.split(" ")
    if not tokens or not tokens[0]:
        return key
    # "GOOGLE LINKEDINCOMMU" must not become a rule on "GOOGLE"
    if tokens[0] in AGGREGATOR_PREFIXES and len(tokens) >= 2:
        return " ".join(tokens[:2])
    if len(tokens[0]) >= 4:
        return tokens[0]
    return " ".join(tokens[:2])


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


def classify(rules: list[dict], normalized: str, mkey: str) -> int | None:
    for rule in rules:
        if rule_matches(rule, normalized, mkey):
            return rule["category_id"]
    return None


def create_rule(conn, pattern: str, category_id: int, match_type: str = "contains",
                priority: int = 10, source: str = "learned") -> int:
    cur = conn.execute(
        "INSERT INTO rules (pattern, match_type, category_id, priority, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pattern.strip(), match_type, category_id, priority, source, utcnow()),
    )
    return cur.lastrowid


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
        cat = classify(rules, row["normalized_desc"], row["merchant_key"])
        if cat is not None:
            conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?",
                         (cat, row["id"]))
            updated += 1
    conn.commit()
    return updated


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
