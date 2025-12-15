from datetime import date, datetime
from pydantic import BaseModel, Field

class AuthGoogleIn(BaseModel):
    server_auth_code: str | None = None
    access_token: str | None = None

class AuthOut(BaseModel):
    access_token: str
    user_email: str

class SyncRequest(BaseModel):
    lookback_days: int | None = None

class SyncStatusOut(BaseModel):
    last_sync_at: datetime | None
    last_history_id: str | None
    queued: bool = False

class TransactionOut(BaseModel):
    id: int
    gmail_message_id: str
    vendor: str | None
    amount: float | None
    currency: str | None
    transaction_date: date | None
    category: str | None
    is_subscription: bool
    trial_end_date: date | None
    renewal_date: date | None

class SubscriptionOut(BaseModel):
    id: int
    vendor_name: str
    amount: float | None
    currency: str | None
    billing_cycle_days: int | None
    last_charge_date: date | None
    next_renewal_date: date | None
    trial_end_date: date | None
    status: str

class RefundDraftIn(BaseModel):
    transaction_id: int
    reason: str = Field(default="I did not intend to purchase this and would like a refund.")
    tone: str = Field(default="polite_firm")

class RefundDraftOut(BaseModel):
    to_email: str | None
    subject: str
    body: str
    facts_used: dict

class DeleteAccountOut(BaseModel):
    deleted: bool = True
