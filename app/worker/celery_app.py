from celery import Celery
from celery.schedules import crontab
from app.config import settings

celery_app = Celery("autopilot", broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND)
celery_app.conf.timezone = "UTC"
celery_app.conf.task_track_started = True

celery_app.conf.beat_schedule = {
    "schedule-alerts-daily": {
        "task": "app.worker.tasks.run_alert_scheduler",
        "schedule": crontab(minute=0, hour=9),
    },
}
