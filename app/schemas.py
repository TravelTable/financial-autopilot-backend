from __future__ import annotations

from datetime import date
import datetime as dt
from pydantic import BaseModel, Field


class AuthGoogleIn(BaseModel):
    server_auth_code: str | None = None
    access_token: str | None = None


class AuthOut(BaseModel):
    access_token: str
    user_email: str


class SyncRequest(BaseModel):
    lookback_days: int | None = None
    force_reprocess: bool = False


class SyncStatusOut(BaseModel):
    last_sync_at: dt.datetime | None
    last_history_id: str | None
    state: str | None = None
    started_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    failed_at: dt.datetime | None = None
    error_message: str | None = None
    queued: bool = False
    in_progress: bool = False


class TransactionOut(BaseModel):
    id: int
    gmail_message_id: str
    vendor: str | None
    amount: float | None
    currency: str | None
    transaction_date: dt.date | None
    category: str | None
    is_subscription: bool
    trial_end_date: dt.date | None
    renewal_date: dt.date | None
    receipt: ReceiptEvidenceOut | None = None
    receipt_confidence: float | None = None


class ReanalyzeTransactionRequest(BaseModel):
    force_llm: bool = False


class SubscriptionOut(BaseModel):
    id: int
    vendor_name: str
    subheader: str | None = None
    amount: float | None
    currency: str | None
    billing_cycle_days: int | None
    last_charge_date: dt.date | None
    next_renewal_date: dt.date | None
    trial_end_date: dt.date | None
    status: str
    next_amount: float | None = None
    amount_is_estimated: bool = False
    price_increased: bool = False
    previous_amount: float | None = None
    price_change_pct: float | None = None
    product_name: str | None = None
    product_id: str | None = None
    provider: str | None = None


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


# ================================
# Subscription insights (pop-up)
# ================================

class EvidenceChargeOut(BaseModel):
    id: int
    date: dt.date | None = None
    amount: float | None = None
    currency: str | None = None


class SubscriptionInsightsOut(BaseModel):
    # Core subscription fields
    id: int
    vendor_name: str
    status: str

    amount: float | None = None
    currency: str | None = None

    billing_cycle_days: int | None = None
    last_charge_date: dt.date | None = None
    next_renewal_date: dt.date | None = None
    trial_end_date: dt.date | None = None

    # Explainable AI insights
    confidence: float
    reasons: list[str]

    # Cadence + prediction
    cadence_days: int | None = None
    cadence_variance_days: float | None = None
    predicted_next_renewal_date: dt.date | None = None
    predicted_is_estimated: bool = False

    # Proof (last charges)
    evidence_charges: list[EvidenceChargeOut] = Field(default_factory=list)


class ReceiptEvidenceOut(BaseModel):
    has_receipt: bool
    provider: str | None = None
    billing_provider: str | None = None
    order_id: str | None = None
    original_order_id: str | None = None
    purchase_date_utc: dt.datetime | None = None
    country: str | None = None
    family_sharing: bool | None = None
    app_name: str | None = None
    subscription_display_name: str | None = None
    developer_or_seller: str | None = None


class SpendingByCategoryOut(BaseModel):
    category: str
    total: float


class SpendingByVendorOut(BaseModel):
    vendor: str
    total: float
    transaction_count: int
    receipt_coverage_rate: float


class SpendingSeriesPointOut(BaseModel):
    year: int
    month: int
    total: float
    subscription_total: float
    general_total: float
    transaction_count: int
    receipt_coverage_rate: float


class SpendingOverviewOut(BaseModel):
    start_date: date | None
    end_date: date | None
    total_spend: float
    subscription_spend: float
    general_spend: float
    subscription_share: float
    transaction_count: int
    subscription_count: int
    general_count: int
    receipt_transaction_count: int
    receipt_coverage_rate: float
    receipt_spend: float
    average_transaction: float | None = None
    largest_transaction: float | None = None
    by_category: list[SpendingByCategoryOut] = Field(default_factory=list)
    by_vendor: list[SpendingByVendorOut] = Field(default_factory=list)
    monthly_series: list[SpendingSeriesPointOut] = Field(default_factory=list)
