from __future__ import annotations

import os
from celery import Celery

from app.config import settings

# IMPORTANT: name can be anything, but keep it stable
celery_app = Celery("financial_autopilot")

# Broker / backend
celery_app.conf.broker_url = settings.CELERY_BROKER_URL
celery_app.conf.result_backend = settings.CELERY_RESULT_BACKEND

# ✅ Make sure tasks are imported/registered
# Either of these patterns works; we’ll do both for safety.
celery_app.conf.imports = ("app.worker.tasks",)
celery_app.autodiscover_tasks(["app.worker"], force=True)

# Optional but good defaults
celery_app.conf.task_track_started = True
celery_app.conf.broker_connection_retry_on_startup = True

# (Optional) If you use JSON only:
celery_app.conf.accept_content = ["json"]
celery_app.conf.task_serializer = "json"
celery_app.conf.result_serializer = "json"

# Debug prints (will show in worker logs)
print(f"✅ Celery broker_url = {celery_app.conf.broker_url}")
print(f"✅ Celery result_backend = {celery_app.conf.result_backend}")
