import logging
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import Notification

logger = logging.getLogger(__name__)


def send_email_delivery(*, recipient_email, subject, message):
    """Deliver an email notification via the configured Django email backend."""
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "notifications@roomloop.edu")
    try:
        sent = send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=[recipient_email],
            fail_silently=False,
        )
        if sent:
            logger.info("Email successfully sent to %s: %s", recipient_email, subject)
            return True
        else:
            logger.warning("Email backend reported 0 messages delivered to %s", recipient_email)
            return False
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", recipient_email, exc)
        return False


def send_push_delivery(*, recipient, title, body):
    """
    Attempt to deliver a push notification to the recipient.
    Explicitly reports missing external push providers or credentials.
    """
    fcm_key = getattr(settings, "FCM_SERVER_KEY", None)
    webpush_settings = getattr(settings, "WEBPUSH_SETTINGS", None)

    if not fcm_key and not webpush_settings:
        logger.info(
            "Push notification skipped for user %s: No push provider or credentials (FCM/VAPID) configured.",
            recipient.pk,
        )
        return False, "Push provider unconfigured (no FCM/VAPID credentials)"

    # If external push service was configured, integration logic would go here
    return False, "Push provider integration pending"


def dispatch_proposal_notification(
    *,
    recipient,
    notification_type: str,
    message: str,
    proposal=None,
    exchange_request=None,
    now=None,
):
    """
    Create a persistent notification in the database and safely schedule
    email and push notification dispatch upon transaction commit.
    
    Retry-safe and idempotent: avoids duplicate delivery within rapid retry windows.
    """
    now = now or timezone.now()

    # De-duplicate identical pending notifications sent within 10 seconds
    recent_duplicate = Notification.objects.filter(
        recipient=recipient,
        notification_type=notification_type,
        proposal=proposal,
        exchange_request=exchange_request,
        created_at__gte=now - timezone.timedelta(seconds=10),
    ).first()

    if recent_duplicate:
        return recent_duplicate

    notification = Notification.objects.create(
        recipient=recipient,
        notification_type=notification_type,
        message=message,
        proposal=proposal,
        exchange_request=exchange_request,
    )

    # Deliver via email and push strictly on transaction commit
    def deliver_channels():
        subject = f"[RoomLoop] {notification.get_notification_type_display()}"
        if recipient.email:
            send_email_delivery(
                recipient_email=recipient.email,
                subject=subject,
                message=message,
            )
        send_push_delivery(
            recipient=recipient,
            title=subject,
            body=message,
        )

    transaction.on_commit(deliver_channels)
    return notification
