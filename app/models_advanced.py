# app/models_advanced.py
from datetime import datetime, date
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Date,
    Boolean,
    Float,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.db import Base


class Vendor(Base):
    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)  # canonical name, e.g. "Spotify"
    normalized_name = Column(String, nullable=False, index=True)  # lowercased, stripped
    website = Column(String, nullable=True)
    support_email = Column(String, nullable=True)
    category = Column(String, nullable=True)  # e.g. "Music", "Streaming"
    created_at = Column(DateTime, default=datetime.utcnow)

    # optional: relationship backrefs from transactions/subscriptions as needed


class SubscriptionPriceHistory(Base):
    __tablename__ = "subscription_price_history"

    id = Column(Integer, primary_key=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), index=True)
    effective_date = Column(Date, nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "effective_date",
            name="uq_subscription_price_per_day",
        ),
    )

    subscription = relationship("Subscription", backref="price_history")


class TransactionAnomaly(Base):
    __tablename__ = "transaction_anomalies"

    id = Column(Integer, primary_key=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), unique=True)
    score = Column(Float, nullable=False)  # 0–1
    label = Column(String, nullable=False)  # e.g. "possible_scam", "unusual_amount"
    reason = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved = Column(Boolean, default=False)

    transaction = relationship("Transaction", backref="anomaly")


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True)
    # alert toggles
    notify_price_increase = Column(Boolean, default=True)
    notify_duplicates = Column(Boolean, default=True)
    notify_anomalies = Column(Boolean, default=True)
    # thresholds
    price_increase_percent_threshold = Column(Float, default=10.0)  # 10%
    anomaly_amount_sigma = Column(Float, default=3.0)  # std dev multiplier
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="settings")
