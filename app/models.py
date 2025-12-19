import enum
from datetime import datetime, date

from sqlalchemy import (
    String, DateTime, Boolean, Integer, BigInteger, ForeignKey,
    Numeric, Text, UniqueConstraint, Enum, Date, JSON
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    canceled = "canceled"
    ignored = "ignored"


class NotificationType(str, enum.Enum):
    trial = "trial"
    renewal = "renewal"
    price_increase = "price_increase"
    anomaly = "anomaly"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    google_accounts = relationship("GoogleAccount", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")


class GoogleAccount(Base):
    __tablename__ = "google_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    google_user_id: Mapped[str] = mapped_column(String(128), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)

    access_token: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text, default="")
    token_expiry_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_history_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="google_accounts")
    emails = relationship("EmailIndex", back_populates="google_account", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("user_id", "google_user_id", name="uq_user_google"),)


class EmailIndex(Base):
    __tablename__ = "emails_index"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_account_id: Mapped[int] = mapped_column(ForeignKey("google_accounts.id"), index=True)

    gmail_message_id: Mapped[str] = mapped_column(String(128), index=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ✅ FIX: Gmail internalDate is epoch milliseconds (~1.7e12) => BIGINT required
    internal_date_ms: Mapped[int] = mapped_column(BigInteger)

    from_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)

    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    google_account = relationship("GoogleAccount", back_populates="emails")

    __table_args__ = (UniqueConstraint("google_account_id", "gmail_message_id", name="uq_gmail_msg"),)


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    domains: Mapped[list | None] = mapped_column(JSON, nullable=True)
    support_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    google_account_id: Mapped[int] = mapped_column(ForeignKey("google_accounts.id"), index=True)

    gmail_message_id: Mapped[str] = mapped_column(String(128), index=True)

    vendor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"), nullable=True)

    amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    transaction_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_subscription: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    renewal_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    confidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parser_version: Mapped[str] = mapped_column(String(32), default="v1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="transactions")

    __table_args__ = (UniqueConstraint("google_account_id", "gmail_message_id", name="uq_tx_gmail_msg"),)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"), nullable=True)
    vendor_name: Mapped[str] = mapped_column(String(256), index=True)

    amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    billing_cycle_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_charge_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_renewal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    trial_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus), default=SubscriptionStatus.active)

    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="subscriptions")


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    type: Mapped[NotificationType] = mapped_column(Enum(NotificationType))
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text)

    scheduled_for: Mapped[datetime] = mapped_column(DateTime, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="notifications")


class AIRun(Base):
    __tablename__ = "ai_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
