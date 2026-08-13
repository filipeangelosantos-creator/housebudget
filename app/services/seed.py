"""Default category tree and starter classification rules.

Everything seeded here is editable in the app (Budgets → Categories and the
Rules page). Seeds only run when the corresponding tables are empty.
"""
from ..db import get_setting, set_setting, utcnow

# Bump when DEFAULT_RULES gains entries, so existing databases pick them up.
SEED_RULES_VERSION = 2

# (group, kind, [(category, excluded)])
DEFAULT_CATEGORIES = [
    ("Income", "income", [
        ("Salary", 0), ("Bonus & Extras", 0), ("Interest & Dividends", 0),
        ("Other Income", 0),
    ]),
    ("Housing", "expense", [
        ("Rent / Mortgage", 0), ("Property Tax", 0), ("Home Insurance", 0),
        ("Electricity & Gas", 0), ("Water", 0), ("Internet & TV", 0),
        ("Mobile Phone", 0), ("Home Maintenance", 0),
    ]),
    ("Food", "expense", [
        ("Groceries", 0), ("Restaurants & Takeout", 0), ("Coffee & Snacks", 0),
    ]),
    ("Transportation", "expense", [
        ("Car Payment", 0), ("Fuel", 0), ("Car Insurance", 0),
        ("Parking & Tolls", 0), ("Public Transit", 0), ("Car Maintenance", 0),
        ("Rideshare & Taxi", 0),
    ]),
    ("Health", "expense", [
        ("Health Insurance", 0), ("Pharmacy", 0), ("Doctor & Dental", 0),
        ("Fitness", 0),
    ]),
    ("Family & Personal", "expense", [
        ("Clothing", 0), ("Personal Care", 0), ("Gifts & Donations", 0),
        ("Childcare & Kids", 0), ("Pets", 0), ("Education", 0),
    ]),
    ("Lifestyle", "expense", [
        ("Entertainment", 0), ("Subscriptions & Streaming", 0),
        ("Travel & Vacation", 0), ("Hobbies", 0), ("Shopping", 0),
    ]),
    ("Financial", "expense", [
        ("Savings & Investments", 0), ("Debt Payment", 0), ("Bank Fees", 0),
        ("Taxes & Government", 0), ("Insurance (Other)", 0),
    ]),
    ("Other", "expense", [
        ("Cash Withdrawal", 0), ("Miscellaneous", 0),
        # Money moving between your own accounts must not count as spending:
        ("Transfers", 1), ("Credit Card Payment", 1),
    ]),
]

# (pattern, category name, priority) — all 'contains', matched on normalized text
DEFAULT_RULES = [
    # Income
    ("PAYROLL", "Salary", 100), ("DIRECT DEP", "Salary", 100),
    ("SALARY", "Salary", 100), ("PAYCHECK", "Salary", 100),
    ("PAYCHEQUE", "Salary", 100), ("TAX REFUND", "Other Income", 90),
    ("INTEREST CHARGE", "Bank Fees", 90), ("INTEREST", "Interest & Dividends", 115),
    # Housing
    ("MORTGAGE", "Rent / Mortgage", 100),
    ("RENT PAYMENT", "Rent / Mortgage", 100),
    # Transfers / CC payments (excluded from budget). Deliberately specific:
    # a bare "AUTO PAY" would swallow a car-loan direct debit, which is a real
    # expense, so only wordings that mean "paying off this card" are listed.
    ("PAYMENT THANK YOU", "Credit Card Payment", 90),
    ("PAYMENT - THANK", "Credit Card Payment", 90),
    ("PAYMENT RECEIVED", "Credit Card Payment", 95),
    ("AUTOPAY", "Credit Card Payment", 95),
    ("AUTO PAYMENT", "Credit Card Payment", 95),
    ("AUTOMATIC PAYMENT", "Credit Card Payment", 95),
    ("MOBILE PAYMENT", "Credit Card Payment", 95),
    ("ONLINE PAYMENT", "Credit Card Payment", 95),
    ("ELECTRONIC PAYMENT", "Credit Card Payment", 95),
    ("EPAYMENT", "Credit Card Payment", 95),
    ("CREDIT CRD", "Credit Card Payment", 95),
    ("CREDIT CARD PAYMENT", "Credit Card Payment", 90),
    ("CARDMEMBER SERV", "Credit Card Payment", 95),
    ("BILL PAYMENT", "Transfers", 105),
    ("TRANSFER", "Transfers", 110), ("XFER", "Transfers", 110),
    ("WEBXFR", "Transfers", 105), ("P2P", "Transfers", 105),
    # Groceries
    ("WALMART", "Groceries", 100), ("WAL-MART", "Groceries", 100),
    ("COSTCO", "Groceries", 100), ("SAFEWAY", "Groceries", 100),
    ("KROGER", "Groceries", 100), ("TRADER JOE", "Groceries", 100),
    ("WHOLE FOODS", "Groceries", 100), ("ALDI", "Groceries", 100),
    ("LIDL", "Groceries", 100), ("SUPERSTORE", "Groceries", 100),
    ("LOBLAW", "Groceries", 100), ("SOBEYS", "Groceries", 100),
    ("NO FRILLS", "Groceries", 100), ("FOOD BASICS", "Groceries", 100),
    ("CONTINENTE", "Groceries", 100), ("PINGO DOCE", "Groceries", 100),
    ("MERCADONA", "Groceries", 100), ("LCBO", "Groceries", 100),
    # Restaurants / coffee
    ("UBER EATS", "Restaurants & Takeout", 90),
    ("DOORDASH", "Restaurants & Takeout", 100),
    ("GRUBHUB", "Restaurants & Takeout", 100),
    ("SKIP THE DISHES", "Restaurants & Takeout", 100),
    ("SKIPTHEDISHES", "Restaurants & Takeout", 100),
    ("MCDONALD", "Restaurants & Takeout", 100),
    ("BURGER KING", "Restaurants & Takeout", 100),
    ("WENDY", "Restaurants & Takeout", 100),
    ("KFC", "Restaurants & Takeout", 100),
    ("CHIPOTLE", "Restaurants & Takeout", 100),
    ("TACO BELL", "Restaurants & Takeout", 100),
    ("PIZZA", "Restaurants & Takeout", 100),
    ("SUBWAY", "Restaurants & Takeout", 100),
    ("RESTAURANT", "Restaurants & Takeout", 110),
    ("STARBUCKS", "Coffee & Snacks", 100),
    ("TIM HORTONS", "Coffee & Snacks", 100),
    ("DUNKIN", "Coffee & Snacks", 100),
    ("7-ELEVEN", "Coffee & Snacks", 100),
    # Transport
    ("UBER", "Rideshare & Taxi", 100), ("LYFT", "Rideshare & Taxi", 100),
    ("SHELL", "Fuel", 100), ("CHEVRON", "Fuel", 100), ("EXXON", "Fuel", 100),
    ("ESSO", "Fuel", 100), ("PETRO", "Fuel", 100), ("MOBIL", "Fuel", 100),
    ("SUNOCO", "Fuel", 100), ("GALP", "Fuel", 100),
    ("PARKING", "Parking & Tolls", 100), ("PAYBYPHONE", "Parking & Tolls", 100),
    ("IMPARK", "Parking & Tolls", 100), ("TOLL", "Parking & Tolls", 110),
    # Subscriptions / entertainment
    ("NETFLIX", "Subscriptions & Streaming", 100),
    ("SPOTIFY", "Subscriptions & Streaming", 100),
    ("DISNEY", "Subscriptions & Streaming", 100),
    ("HBO", "Subscriptions & Streaming", 100),
    ("HULU", "Subscriptions & Streaming", 100),
    ("PRIME VIDEO", "Subscriptions & Streaming", 100),
    ("YOUTUBE PREMIUM", "Subscriptions & Streaming", 100),
    ("APPLE.COM/BILL", "Subscriptions & Streaming", 100),
    ("GOOGLE ONE", "Subscriptions & Streaming", 100),
    ("CINEPLEX", "Entertainment", 100), ("CINEMA", "Entertainment", 100),
    ("AMC #", "Entertainment", 100), ("STEAM GAMES", "Entertainment", 100),
    ("STEAMGAMES", "Entertainment", 100), ("PLAYSTATION", "Entertainment", 100),
    ("NINTENDO", "Entertainment", 100), ("XBOX", "Entertainment", 100),
    # Shopping / home
    ("AMZN", "Shopping", 100), ("AMAZON", "Shopping", 100),
    ("BEST BUY", "Shopping", 100), ("TARGET", "Shopping", 100),
    ("IKEA", "Home Maintenance", 100), ("HOME DEPOT", "Home Maintenance", 100),
    ("LOWES", "Home Maintenance", 100), ("LEROY MERLIN", "Home Maintenance", 100),
    ("ZARA", "Clothing", 100), ("H&M", "Clothing", 100),
    ("UNIQLO", "Clothing", 100), ("OLD NAVY", "Clothing", 100),
    ("WINNERS", "Clothing", 100), ("MARSHALLS", "Clothing", 100),
    # Health / fitness
    ("PHARMACY", "Pharmacy", 100), ("CVS", "Pharmacy", 100),
    ("WALGREENS", "Pharmacy", 100), ("SHOPPERS DRUG MART", "Pharmacy", 100),
    ("FARMACIA", "Pharmacy", 100),
    ("PLANET FITNESS", "Fitness", 100), ("GOODLIFE", "Fitness", 100),
    ("LA FITNESS", "Fitness", 100), ("ANYTIME FITNESS", "Fitness", 100),
    ("GYM", "Fitness", 110),
    # Utilities / phone
    ("HYDRO", "Electricity & Gas", 100), ("ELECTRIC", "Electricity & Gas", 110),
    ("EDP COMERCIAL", "Electricity & Gas", 100),
    ("COMCAST", "Internet & TV", 100), ("XFINITY", "Internet & TV", 100),
    ("SPECTRUM", "Internet & TV", 100),
    ("ROGERS", "Mobile Phone", 100), ("TELUS", "Mobile Phone", 100),
    ("BELL MOBILITY", "Mobile Phone", 100), ("T-MOBILE", "Mobile Phone", 100),
    ("VODAFONE", "Mobile Phone", 100), ("AT&T", "Mobile Phone", 100),
    ("VERIZON", "Mobile Phone", 100),
    # Travel
    ("AIRBNB", "Travel & Vacation", 100), ("EXPEDIA", "Travel & Vacation", 100),
    ("BOOKING.COM", "Travel & Vacation", 100), ("HOTEL", "Travel & Vacation", 110),
    ("MARRIOTT", "Travel & Vacation", 100), ("HILTON", "Travel & Vacation", 100),
    ("AIR CANADA", "Travel & Vacation", 100), ("TAP AIR", "Travel & Vacation", 100),
    ("DELTA AIR", "Travel & Vacation", 100), ("UNITED AIR", "Travel & Vacation", 100),
    ("RYANAIR", "Travel & Vacation", 100), ("EASYJET", "Travel & Vacation", 100),
    # Pets
    ("PETSMART", "Pets", 100), ("PET VALU", "Pets", 100), ("PETCO", "Pets", 100),
    # Fees / cash
    ("NSF FEE", "Bank Fees", 100), ("MONTHLY FEE", "Bank Fees", 100),
    ("SERVICE CHARGE", "Bank Fees", 100), ("OVERDRAFT", "Bank Fees", 100),
    ("ANNUAL FEE", "Bank Fees", 100),
    ("ATM", "Cash Withdrawal", 100), ("CASH WITHDRAWAL", "Cash Withdrawal", 100),
    ("LEVANTAMENTO", "Cash Withdrawal", 100),
    ("INSURANCE", "Insurance (Other)", 120),
]


def seed_defaults(conn) -> None:
    if conn.execute("SELECT COUNT(*) FROM category_groups").fetchone()[0] == 0:
        for gi, (gname, kind, cats) in enumerate(DEFAULT_CATEGORIES):
            cur = conn.execute(
                "INSERT INTO category_groups (name, kind, sort_order) VALUES (?, ?, ?)",
                (gname, kind, gi))
            gid = cur.lastrowid
            for ci, (cname, excluded) in enumerate(cats):
                conn.execute(
                    "INSERT INTO categories (group_id, name, sort_order, excluded) "
                    "VALUES (?, ?, ?, ?)", (gid, cname, ci, excluded))
        conn.commit()

    ensure_seed_rules(conn)

    if not conn.execute("SELECT 1 FROM settings WHERE key = 'currency'").fetchone():
        conn.execute("INSERT INTO settings (key, value) VALUES ('currency', '$')")
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('household', 'Our Budget')")
        conn.commit()


def ensure_seed_rules(conn) -> int:
    """Install any built-in rules the database doesn't have yet.

    Without this, a database created by an earlier release would never see
    rules added later — the original seeding only ran on an empty table. Only
    runs when SEED_RULES_VERSION moves, and never touches or duplicates a rule
    whose pattern already exists, so edits and additions of your own survive.
    """
    if int(get_setting(conn, "seed_rules_version", "0") or 0) >= SEED_RULES_VERSION:
        return 0

    existing = {r["pattern"].strip().upper()
                for r in conn.execute("SELECT pattern FROM rules").fetchall()}
    cat_ids = {r["name"]: r["id"] for r in
               conn.execute("SELECT id, name FROM categories").fetchall()}
    now = utcnow()
    added = 0
    for pattern, cat_name, priority in DEFAULT_RULES:
        cat_id = cat_ids.get(cat_name)
        if not cat_id or pattern.strip().upper() in existing:
            continue
        conn.execute(
            "INSERT INTO rules (pattern, match_type, category_id, priority, "
            "source, created_at) VALUES (?, 'contains', ?, ?, 'seed', ?)",
            (pattern, cat_id, priority, now))
        existing.add(pattern.strip().upper())
        added += 1
    conn.commit()
    set_setting(conn, "seed_rules_version", str(SEED_RULES_VERSION))

    if added:
        # New rules should reach the backlog, not just future imports.
        from . import classify
        classify.apply_rules_to_uncategorized(conn)
    return added
