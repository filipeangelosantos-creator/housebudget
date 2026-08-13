"""Classification rules management."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..deps import current_user, get_conn, render, verify_csrf
from ..services import classify

router = APIRouter()


SOURCES = ("seed", "user", "learned")


@router.get("/rules")
def rules_page(request: Request, conn=Depends(get_conn), user=Depends(current_user),
               q: str = "", source: str = ""):
    """The rule list, searchable. With a couple of hundred seed rules, finding
    the one that filed something wrong is the whole job."""
    where, params = [], []
    if q.strip():
        # Matching the category name too: "which rules send things to Fuel?"
        where.append("(r.pattern LIKE ? OR c.name LIKE ?)")
        params.extend([f"%{q.strip()}%", f"%{q.strip()}%"])
    if source in SOURCES:
        where.append("r.source = ?")
        params.append(source)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rules = conn.execute(
        f"SELECT r.*, c.name AS category_name FROM rules r "
        f"JOIN categories c ON c.id = r.category_id {where_sql} "
        f"ORDER BY r.priority, r.pattern", params).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    categories = conn.execute(
        "SELECT c.id, c.name, g.name AS group_name FROM categories c "
        "JOIN category_groups g ON g.id = c.group_id WHERE c.archived = 0 "
        "ORDER BY g.sort_order, c.sort_order").fetchall()
    uncat = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL").fetchone()[0]
    dupes = classify.duplicate_groups(conn)
    return render(request, conn, "rules.html", rules=rules, categories=categories,
                  uncat=uncat, q=q, source=source if source in SOURCES else "",
                  total=total, usage=classify.rule_usage(conn), dupes=dupes,
                  redundant=sum(len(g["redundant"]) for g in dupes))


@router.post("/rules/dedupe", dependencies=[Depends(verify_csrf)])
def rules_dedupe(request: Request, conn=Depends(get_conn), user=Depends(current_user),
                 include_conflicting: str = Form("")):
    n = classify.remove_duplicate_rules(conn, include_conflicting=bool(include_conflicting))
    return RedirectResponse(
        f"/rules?m=Removed+{n}+rule{'' if n == 1 else 's'}+that+could+never+fire.",
        status_code=303)


@router.post("/rules/{rule_id}/keep", dependencies=[Depends(verify_csrf)])
def rules_keep(rule_id: int, request: Request, conn=Depends(get_conn),
               user=Depends(current_user)):
    """Resolve one conflicting group by keeping this rule and dropping its twins."""
    n = classify.keep_only(conn, rule_id)
    return RedirectResponse(f"/rules?m=Removed+{n}+conflicting+duplicate"
                            f"{'' if n == 1 else 's'}.", status_code=303)


@router.get("/rules/match-count")
def rules_match_count(request: Request, conn=Depends(get_conn),
                      user=Depends(current_user), pattern: str = "",
                      match_type: str = "contains"):
    """How many transactions a pattern would catch, for the live hint shown
    while you edit it. A pattern that is too short catches everything."""
    pattern = pattern.strip()
    if not pattern or match_type not in ("contains", "exact", "regex"):
        return {"count": 0, "total": 0}
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return {"count": classify.count_rule_matches(conn, pattern, match_type),
            "total": total}


@router.post("/rules/add", dependencies=[Depends(verify_csrf)])
def rule_add(request: Request, conn=Depends(get_conn), user=Depends(current_user),
             pattern: str = Form(...), category_id: int = Form(...),
             match_type: str = Form("contains"), priority: int = Form(50)):
    if pattern.strip() and match_type in ("contains", "exact", "regex"):
        rule_id = classify.create_rule(conn, pattern, category_id, match_type,
                                       priority, source="user")
        conn.commit()
        classify.apply_rules_to_uncategorized(conn, only_rule_id=rule_id)
    return RedirectResponse("/rules", status_code=303)


@router.post("/rules/{rule_id}/delete", dependencies=[Depends(verify_csrf)])
def rule_delete(rule_id: int, request: Request, conn=Depends(get_conn),
                user=Depends(current_user)):
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    conn.commit()
    return RedirectResponse("/rules", status_code=303)


@router.post("/rules/rerun", dependencies=[Depends(verify_csrf)])
def rules_rerun(request: Request, conn=Depends(get_conn), user=Depends(current_user)):
    classify.apply_rules_to_uncategorized(conn)
    return RedirectResponse("/rules", status_code=303)
