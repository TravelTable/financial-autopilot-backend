from __future__ import annotations
import io, csv, zipfile
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.deps import get_current_user_id
from app.db import get_db
from app.models import User, Transaction, Subscription
from app.schemas import DeleteAccountOut

router = APIRouter(prefix="/privacy", tags=["privacy"])

@router.get("/export")
def export_data(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    txs = db.execute(select(Transaction).where(Transaction.user_id == user_id)).scalars().all()
    subs = db.execute(select(Subscription).where(Subscription.user_id == user_id)).scalars().all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        tx_csv = io.StringIO()
        wtx = csv.writer(tx_csv)
        wtx.writerow(["id","gmail_message_id","vendor","amount","currency","transaction_date","category","is_subscription","trial_end_date","renewal_date"])
        for t in txs:
            wtx.writerow([t.id, t.gmail_message_id, t.vendor, t.amount, t.currency, t.transaction_date, t.category, t.is_subscription, t.trial_end_date, t.renewal_date])
        z.writestr("transactions.csv", tx_csv.getvalue())

        s_csv = io.StringIO()
        ws = csv.writer(s_csv)
        ws.writerow(["id","vendor_name","amount","currency","billing_cycle_days","last_charge_date","next_renewal_date","trial_end_date","status"])
        for s in subs:
            ws.writerow([s.id, s.vendor_name, s.amount, s.currency, s.billing_cycle_days, s.last_charge_date, s.next_renewal_date, s.trial_end_date, getattr(s.status, "value", str(s.status))])
        z.writestr("subscriptions.csv", s_csv.getvalue())

        z.writestr("user.txt", f"email={user.email if user else ''}\n")

    return Response(content=buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": "attachment; filename=export.zip"})

@router.delete("/account", response_model=DeleteAccountOut)
def delete_account(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        db.delete(user)
        db.commit()
    return DeleteAccountOut(deleted=True)
