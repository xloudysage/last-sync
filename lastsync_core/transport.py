"""Bounded HTTP requests and safe service errors."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .errors import ServiceError

TIMEOUT = httpx.Timeout(8, connect=4)


def retry_after(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def request_json(client, service, method, url, **kwargs):
    try:
        response = client.request(method, url, timeout=TIMEOUT, **kwargs)
    except httpx.RequestError:
        raise ServiceError(service, f"{service}: network unavailable; will retry",
                           uncertain=method != "GET") from None
    code = response.status_code
    if code >= 400:
        messages = {
            401: "authorization expired; reconnect in Settings",
            403: "access denied; check Premium, scopes and app allowlist",
            404: "resource or active playback device unavailable",
            429: "rate limited; waiting before retry",
        }
        detail = messages.get(code, "service unavailable" if code >= 500 else "request rejected")
        raise ServiceError(service, f"{service}: {detail}", status=code,
                           retry_after=retry_after(response.headers.get("Retry-After")),
                           fatal=code in (400, 401, 403),
                           uncertain=method != "GET" and code >= 500)
    if code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        raise ServiceError(service, f"{service}: invalid JSON response",
                           uncertain=method != "GET") from None
