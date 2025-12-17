from celery import Celery
from celery.schedules import crontab
import os

# FORCE Redis. No fallbacks. No AMQP.
broker_url = os.environ["CELERY_BROKER_URL"]
result_backend = os.environ.get("CELERY_RESULT_BACKEND", broker_url)

celery_app = Celery(
    "financial_autopilot",
    broker=broker_url,
    backend=result_backend,
)

celery_app.conf.update(
    timezone="UTC",
    task_track_started=True,
)

print("✅ Celery broker_url =", broker_url)
print("✅ Celery result_backend =", result_backend)
