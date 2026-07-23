"""Outbound-only optional notification adapters."""
from __future__ import annotations

from .config import (
    NotificationConfigError,
    disabled_notification_config,
    load_notification_config,
    parse_notification_config,
)
from .dispatcher import dispatch_notifications
from .events import (
    create_notification_event,
    notification_from_lifecycle,
    notification_line,
    notification_payload,
)
from .formatters import format_notification
from .http import notify_https, public_address
from .local import deliver_desktop, deliver_local_command
from .queue import (
    enqueue_lifecycle_notification,
    enqueue_notification,
    process_notification_queue,
)


__all__ = [
    "NotificationConfigError",
    "create_notification_event",
    "disabled_notification_config",
    "dispatch_notifications",
    "deliver_desktop",
    "deliver_local_command",
    "enqueue_lifecycle_notification",
    "enqueue_notification",
    "format_notification",
    "load_notification_config",
    "notification_line",
    "notification_from_lifecycle",
    "notification_payload",
    "notify_https",
    "parse_notification_config",
    "process_notification_queue",
    "public_address",
]
