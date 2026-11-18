import logging
from celery import shared_task
from django.utils import timezone

from .services import (
    expire_exchange_proposals,
    expire_exchange_requests,
    reconcile_all_institutions_matching,
    run_institution_matching,
)

logger = logging.getLogger(__name__)


@shared_task(name="apps.exchange.tasks.expire_exchange_requests_task")
def expire_exchange_requests_task():
    """Periodic Celery task to expire all overdue open exchange requests."""
    count, expired_ids = expire_exchange_requests(now=timezone.now())
    return {
        "status": "success",
        "expired_count": count,
        "expired_ids": expired_ids,
    }


@shared_task(name="apps.exchange.tasks.expire_exchange_proposals_task")
def expire_exchange_proposals_task():
    """Periodic Celery task to expire all overdue pending exchange proposals."""
    count, expired_ids = expire_exchange_proposals(now=timezone.now())
    return {
        "status": "success",
        "expired_count": count,
        "expired_ids": expired_ids,
    }


@shared_task(
    name="apps.exchange.tasks.run_institution_matching_task",
    bind=True,
    max_retries=3,
    default_retry_delay=5,
)
def run_institution_matching_task(self, institution_id, trigger_reason="celery_task", trigger_request_id=None):
    """Background task executing institution-scoped matching."""
    try:
        job = run_institution_matching(
            institution_id=institution_id,
            trigger_reason=trigger_reason,
            trigger_request_id=trigger_request_id,
        )
        return {
            "status": "success",
            "job_id": str(job.id),
            "job_status": job.status,
            "candidates_found": job.candidates_found_count,
            "proposals_activated": job.proposals_activated_count,
        }
    except Exception as exc:
        logger.exception("Error executing run_institution_matching_task for institution %s: %s", institution_id, exc)
        raise self.retry(exc=exc)


@shared_task(name="apps.exchange.tasks.reconcile_all_institutions_matching_task")
def reconcile_all_institutions_matching_task():
    """Periodic Celery task for missed events, broker failure recovery, and state reconciliation."""
    scheduled = reconcile_all_institutions_matching()
    return {
        "status": "success",
        "reconciled_institutions_count": len(scheduled),
        "institution_ids": [str(i) for i in scheduled],
    }
