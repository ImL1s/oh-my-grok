"""Pure destination-neutral notification payload formatters."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from omg_cli.notify.events import notification_line, notification_payload


Destination = Literal["telegram", "discord", "slack"]


def format_notification(event: Mapping[str, Any], destination: Destination) -> dict[str, str]:
    """Return a webhook-vendor compatible body without destination secrets."""

    safe_event = notification_payload(event)
    if safe_event is None:
        raise ValueError("notification event failed integrity validation")
    line = notification_line(safe_event)
    if destination == "telegram":
        return {"text": line}
    if destination == "discord":
        return {"content": line}
    if destination == "slack":
        return {"text": line}
    raise ValueError("unsupported notification formatter")


__all__ = ["format_notification"]
