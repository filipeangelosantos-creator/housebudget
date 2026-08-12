"""Reviewing and managing transfers between your own accounts."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..deps import current_user, get_conn, render, verify_csrf
from ..services import transfers

router = APIRouter()


@router.get("/transfers")
def transfers_page(request: Request, conn=Depends(get_conn),
                   user=Depends(current_user)):
    return render(request, conn, "transfers.html",
                  suggestions=transfers.suggestions(conn),
                  group_sizes=transfers.group_sizes(conn),
                  linked=transfers.linked_pairs(conn),
                  linked_count=transfers.linked_count(conn))


@router.post("/transfers/link-group", dependencies=[Depends(verify_csrf)])
def transfers_link_group(request: Request, conn=Depends(get_conn),
                         user=Depends(current_user), group_key: str = Form(...),
                         action: str = Form("link")):
    if action == "dismiss":
        n = transfers.dismiss_group(conn, group_key)
        return RedirectResponse(f"/transfers?m=Dismissed+{n}+pairs.", status_code=303)
    n = transfers.link_group(conn, group_key)
    return RedirectResponse(f"/transfers?m=Linked+{n}+transfers.", status_code=303)


@router.post("/transfers/scan", dependencies=[Depends(verify_csrf)])
def transfers_scan(request: Request, conn=Depends(get_conn),
                   user=Depends(current_user)):
    n = transfers.auto_link(conn)
    return RedirectResponse(f"/transfers?m=Linked+{n}+transfer{'' if n == 1 else 's'}.",
                            status_code=303)


@router.post("/transfers/link", dependencies=[Depends(verify_csrf)])
def transfers_link(request: Request, conn=Depends(get_conn),
                   user=Depends(current_user), out_txn_id: int = Form(...),
                   in_txn_id: int = Form(...)):
    transfers.link(conn, out_txn_id, in_txn_id, source="manual")
    return RedirectResponse("/transfers", status_code=303)


@router.post("/transfers/dismiss", dependencies=[Depends(verify_csrf)])
def transfers_dismiss(request: Request, conn=Depends(get_conn),
                      user=Depends(current_user), out_txn_id: int = Form(...),
                      in_txn_id: int = Form(...)):
    transfers.dismiss(conn, out_txn_id, in_txn_id)
    return RedirectResponse("/transfers", status_code=303)


@router.post("/transfers/{link_id}/unlink", dependencies=[Depends(verify_csrf)])
def transfers_unlink(link_id: int, request: Request, conn=Depends(get_conn),
                     user=Depends(current_user)):
    transfers.unlink(conn, link_id)
    return RedirectResponse("/transfers", status_code=303)
