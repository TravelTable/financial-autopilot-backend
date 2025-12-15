from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.deps import get_current_user_id
from app.db import get_db
from app.schemas import RefundDraftIn, RefundDraftOut
from app.refunds import create_refund_draft

router = APIRouter(prefix="/refunds", tags=["refunds"])

@router.post("/draft", response_model=RefundDraftOut)
def draft_refund(req: RefundDraftIn, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    try:
        d = create_refund_draft(db, user_id=user_id, transaction_id=req.transaction_id, reason=req.reason, tone=req.tone)
        return RefundDraftOut(**d)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
