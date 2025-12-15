# app/tasks/alerts.py
from celery import shared_task
from datetime import date
from sqlalchemy.orm import Session
from app.db import SessionLocal
from app.models import User, Subscription, Transaction
from app.models_advanced import UserSettings
from app.services.subscription_analysis import (
    detect_price_increase,
    find_duplicate_subscriptions,
)
from app.services.anomaly_detector import score_transaction_anomaly
from app.services.notifications import create_notification  # you implement this


@shared_task
def run_price_increase_checks():
    db: Session = SessionLocal()
    try:
        users = db.query(User).all()
        for user in users:
            settings = (
                db.query(UserSettings)
                .filter(UserSettings.user_id == user.id)
                .first()
            )
            if settings and not settings.notify_price_increase:
                continue

            subs = (
                db.query(Subscription)
                .filter(Subscription.user_id == user.id, Subscription.status == "active")
                .all()
            )
            for s in subs:
                has_inc, old_price, new_price = detect_price_increase(db, s)
                if not has_inc:
                    continue
                create_notification(
                    db=db,
                    user=user,
                    kind="price_increase",
                    title="Subscription price increased",
                    body=f"{s.name} increased from {old_price:.2f} to {new_price:.2f}",
                    metadata={"subscription_id": s.id},
                )
    finally:
        db.close()


@shared_task
def run_duplicate_subscription_checks():
    db: Session = SessionLocal()
    try:
        users = db.query(User).all()
        for user in users:
            settings = (
                db.query(UserSettings)
                .filter(UserSettings.user_id == user.id)
                .first()
            )
            if settings and not settings.notify_duplicates:
                continue

            groups = find_duplicate_subscriptions(db, user)
            for group in groups:
                names = ", ".join(s.name for s in group)
                create_notification(
                    db=db,
                    user=user,
                    kind="duplicate_subscriptions",
                    title="Possible duplicate subscriptions",
                    body=f"You may be paying for multiple similar subscriptions: {names}",
                    metadata={"subscription_ids": [s.id for s in group]},
                )
    finally:
        db.close()


@shared_task
def run_anomaly_checks():
    db: Session = SessionLocal()
    try:
        users = db.query(User).all()
        for user in users:
            settings = (
                db.query(UserSettings)
                .filter(UserSettings.user_id == user.id)
                .first()
            )
            if settings and not settings.notify_anomalies:
                continue

            transactions = (
                db.query(Transaction)
                .filter(Transaction.user_id == user.id)
                .order_by(Transaction.date.desc())
                .limit(200)
                .all()
            )
            for tx in transactions:
                anomaly = score_transaction_anomaly(db, user, tx)
                if anomaly and anomaly.score >= 0.7:
                    create_notification(
                        db=db,
                        user=user,
                        kind="transaction_anomaly",
                        title="Unusual transaction detected",
                        body=anomaly.reason,
                        metadata={"transaction_id": tx.id},
                    )
    finally:
        db.close()
