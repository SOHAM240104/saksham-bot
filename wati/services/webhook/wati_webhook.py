import asyncio
import json
import logging
from urllib.parse import quote

import httpx

from wati.settings import settings

from .parse import _timeline_items_from_response, parse_wati_timeline_for_thread

logger = logging.getLogger(__name__)


def wati_response_indicates_success(response: httpx.Response) -> bool:
    if response.status_code >= 400:
        return False
    try:
        data = response.json()
    except Exception:
        return True
    if isinstance(data, dict) and data.get("result") is False:
        return False
    if isinstance(data, dict) and data.get("ok") is False:
        return False
    return True


def wati_outbound_message_id(response: httpx.Response) -> str | None:
    """Extract WATI/WhatsApp message id from a successful send response.

    Poll stores this on the welcome Message row for deduplication and tracing.
    """
    if not wati_response_indicates_success(response):
        return None
    try:
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    msg = data.get("message")
    if isinstance(msg, dict):
        for key in ("whatsappMessageId", "id", "localMessageId"):
            val = str(msg.get(key) or "").strip()
            if val:
                return val
    return None

def _wati_tenant_api_base() -> str:
    base = settings.WATI_API_ENDPOINT.rstrip("/")
    tenant = (getattr(settings, "WATI_TENANT_ID", None) or "").strip()
    if tenant and not base.rstrip("/").endswith(tenant):
        return f"{base}/{tenant}"
    return base


def _wati_v1_api_base() -> str:
    return _wati_tenant_api_base()


def _wati_ext_v3_api_base() -> str:
    """ext v3 resolves tenant from Bearer token — URL must not include tenant id."""
    base = settings.WATI_API_ENDPOINT.rstrip("/")
    tenant = (getattr(settings, "WATI_TENANT_ID", None) or "").strip()
    if tenant and base.endswith(f"/{tenant}"):
        return base[: -(len(tenant) + 1)]
    last_segment = base.rsplit("/", 1)[-1]
    if last_segment.isdigit():
        return base.rsplit("/", 1)[0]
    return base


async def fetch_wati_get_messages(phone: str, *, page_size: int = 5) -> dict | None:
    """Fetch WATI conversation timeline for a phone number.

    Poll uses a small page_size (default 5) — only recent ticket events matter
    for detecting agent close; keeps API payload small.
    """
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    if not digits:
        return None
    url = f"{_wati_v1_api_base()}/api/v1/getMessages/{quote(digits, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
                    "Accept": "application/json",
                },
                params={"pageNumber": 1, "pageSize": max(1, min(page_size, 50))},
            )
            logger.info("WATI getMessages status=%s url=%s", r.status_code, url[:140])
            if r.status_code >= 400:
                logger.warning("WATI getMessages body=%s", r.text[:500])
                return None
            data = r.json()
            return data
    except Exception:
        logger.exception("WATI getMessages failed")
        return None


async def read_wati_timeline_state(phone: str) -> str | None:
    """Map WATI timeline → ``"resolved"`` | ``"assigned"`` for TechSaathi poll.

    HOW:
    1. GET /api/v1/getMessages (last 5 items)
    2. Extract ticket events via ``_timeline_items_from_response``
    3. ``parse_wati_timeline_for_thread`` decides human still active vs closed

    Returns None if disabled (``WATI_EXT_MESSAGES_ENABLED``) or API failed.
    """
    if not getattr(settings, "WATI_EXT_MESSAGES_ENABLED", True):
        return None
    data = await fetch_wati_get_messages(phone, page_size=5)
    if not isinstance(data, dict):
        return None
    res = data.get("result")
    if res is not None and str(res).lower() not in ("success", "true", "1"):
        if res is False:
            logger.warning("WATI getMessages unexpected result=%s", res)
            return None
    items = _timeline_items_from_response(data)
    ext_state = parse_wati_timeline_for_thread(items)
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    # Debug logging — helps trace poll decisions in production.
    ticket_items = [
        {
            "eventType": it.get("eventType"),
            "eventDescription": it.get("eventDescription"),
            "created": it.get("created"),
            "ticketId": it.get("ticketId"),
        }
        for it in items
        if isinstance(it, dict) and str(it.get("eventType") or "").lower() == "ticket"
    ]
    logger.info(
        "WATI getMessages timeline phone=%s ext_state=%s ticket_events=%s",
        digits,
        ext_state,
        json.dumps(ticket_items, ensure_ascii=False),
    )
    payload_preview = json.dumps(data, indent=2, ensure_ascii=False)
    if len(payload_preview) > 8000:
        payload_preview = payload_preview[:8000] + "\n... (truncated)"
    logger.info(
        "WATI getMessages payload phone=%s:\n%s",
        digits,
        payload_preview,
    )
    return ext_state

# =========================================================
# TYPING INDICATOR (ext v3)
# =========================================================
def _typing_indicator_target(phone: str) -> str:
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    channel = (getattr(settings, "WATI_CHANNEL_NUMBER", None) or "").strip()
    if channel and digits:
        return f"{channel}:{digits}"
    return digits


async def send_typing_indicator(phone: str) -> bool:
    if not getattr(settings, "WATI_TYPING_INDICATOR_ENABLED", True):
        return False
    target = _typing_indicator_target(phone)
    if not target:
        return False
    url = f"{_wati_ext_v3_api_base()}/api/ext/v3/conversations/typingIndicator"
    headers = {
        "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                headers=headers,
                json={"target": target},
            )
            logger.info(
                "WATI typingIndicator status=%s target=%s text=%s",
                response.status_code,
                target,
                response.text[:200],
            )
            return wati_response_indicates_success(response)
    except Exception:
        logger.exception("WATI typingIndicator failed target=%s", target)
        return False


async def keep_typing_indicator(phone: str, stop: asyncio.Event) -> None:
    """Refresh typing every ~20s; WATI auto-dismisses after 25s."""
    try:
        while not stop.is_set():
            await send_typing_indicator(phone)
            try:
                await asyncio.wait_for(stop.wait(), timeout=20.0)
                return
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        return


# =========================================================
# SEND MESSAGE TO WATI
# =========================================================
async def send_message(phone: str, message: str) -> bool:
    if not phone:
        logger.warning("Skipping send_message: phone missing")
        return False

    text = (message or "").strip()
    if not text:
        logger.warning("Empty message, not sending")
        return False

    url = f"{settings.WATI_API_ENDPOINT}/api/v1/sendSessionMessage/{phone}"
    headers = {
        "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    params={"messageText": text},
                )
                logger.info(
                    "WATI send response status=%s text=%s",
                    response.status_code,
                    response.text,
                )
                return wati_response_indicates_success(response)

        except httpx.TimeoutException:
            logger.warning("Timeout attempt %s for %s", attempt, phone)
            await asyncio.sleep(attempt)

        except Exception as exc:
            logger.exception("Send failed attempt %s: %s", attempt, exc)
            await asyncio.sleep(attempt)

    return False


async def send_interactive_buttons_message(
    phone: str,
    message: str,
    action: str,
) -> str | None:
    """Send interactive buttons. Returns WATI message id on success, else None.

    Poll uses the returned id when persisting the post-handoff welcome Message.
    """
    if not phone:
        logger.warning("Skipping interactive message: phone missing")
        return None

    text = (message or "").strip()
    if not text:
        logger.warning("Skipping interactive message: empty body")
        return None

    if action == "mode_buttons":
        buttons = [
            {"id": "tech", "title": "Tech Help"},
            {"id": "scam", "title": "Scam Help"},
        ]
    elif action == "feedback_buttons":
        buttons = [
            {"id": "resolved", "title": "Resolved "},
            {"id": "not_resolved", "title": "Still Stuck "},
        ]
    elif action == "scam_os_buttons":
        buttons = [
            {"id": "ios", "title": "iPhone / iPad"},
            {"id": "android", "title": "Android"},
        ]
    else:
        logger.warning("Unknown interactive action=%s", action)
        return None

    url = f"{settings.WATI_API_ENDPOINT}/api/v1/sendInteractiveButtonsMessage"
    headers = {
        "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "body": text,
        "footer": "",
        "buttons": [
            {
                "text": button["title"],
            }
            for button in buttons
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers=headers,
                params={"whatsappNumber": phone},
                json=payload,
            )
            logger.info(
                "WATI interactive response status=%s text=%s",
                response.status_code,
                response.text,
            )
            return wati_outbound_message_id(response)
    except Exception:
        logger.exception("Failed sending interactive buttons action=%s", action)
        return None


async def send_interactive_platform_list_message(phone: str, message: str) -> bool:
    """WhatsApp list message for supported phone brands (up to 10 rows in one message)."""
    if not phone:
        logger.warning("Skipping platform list message: phone missing")
        return False

    text = (message or "").strip()
    if not text:
        logger.warning("Skipping platform list message: empty body")
        return False

    url = f"{settings.WATI_API_ENDPOINT}/api/v1/sendInteractiveListMessage"
    headers = {
        "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "header": "Phone brand",
        "body": text,
        "footer": "",
        "buttonText": "Choose phone",
        "sections": [
            {
                "title": "Supported phones",
                "rows": [
                    {"id": "apple", "title": "Apple", "description": "iPhone / iPad"},
                    {"id": "samsung", "title": "Samsung", "description": "Galaxy"},
                    {"id": "pixel", "title": "Pixel", "description": "Google Pixel"},
                    {"id": "oppo", "title": "Oppo", "description": ""},
                    {"id": "xiaomi", "title": "Xiaomi", "description": "Redmi / POCO"},
                ],
            }
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers=headers,
                params={"whatsappNumber": phone},
                json=payload,
            )
            logger.info(
                "WATI interactive list response status=%s text=%s",
                response.status_code,
                response.text,
            )
            return wati_response_indicates_success(response)
    except Exception:
        logger.exception("Failed sending interactive platform list message")
        return False


async def send_template_message(phone: str, parameters: list[str] | None = None) -> bool:
    if not phone:
        return False

    template_name = getattr(settings, "WATI_TEMPLATE_NAME", "tech_saathi_welcome")
    payload = {
        "template_name": template_name,
        "broadcast_name": "session_start",
        "parameters": [
            {"name": f"{{{{{idx + 1}}}}}", "value": value}
            for idx, value in enumerate(parameters or [])
        ],
    }
    channel_number = getattr(settings, "WATI_CHANNEL_NUMBER", None)
    if channel_number:
        payload["channel_number"] = channel_number

    url = f"{settings.WATI_API_ENDPOINT}/api/v1/sendTemplateMessage"
    headers = {
        "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers=headers,
                params={"whatsappNumber": phone},
                json=payload,
            )
            logger.info(
                "WATI template response status=%s text=%s",
                response.status_code,
                response.text,
            )
            return wati_response_indicates_success(response)
    except Exception:
        logger.exception("Failed sending template message")
        return False
