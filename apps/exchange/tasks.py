from celery import shared_task
from django.utils import timezone

from .services import expire_exchange_requests


@shared_task(name="apps.exchange.tasks.expire_exchange_requests_task")
def expire_exchange_requests_task():
    """Periodic Celery task to expire all overdue open exchange requests."""
    count, expired_ids = expire_exchange_requests(now=timezone.now())
    return {
        "status": "success",
        "expired_count": count,
        "expired_ids": expired_ids,
    }
