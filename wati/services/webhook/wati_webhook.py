import asyncio
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

def _wati_v1_api_base() -> str:
    base = settings.WATI_API_ENDPOINT.rstrip("/")
    tenant = (getattr(settings, "WATI_TENANT_ID", None) or "").strip()
    if tenant and not base.rstrip("/").endswith(tenant):
        return f"{base}/{tenant}"
    return base


async def fetch_wati_get_messages(phone: str) -> dict | None:
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
                params={"pageNumber": 1, "pageSize": 200},
            )
            logger.info("WATI getMessages status=%s url=%s", r.status_code, url[:140])
            if r.status_code >= 400:
                logger.warning("WATI getMessages body=%s", r.text[:500])
                return None
            return r.json()
    except Exception:
        logger.exception("WATI getMessages failed")
        return None


async def read_wati_timeline_state(phone: str) -> str | None:
    """resolved | assigned for techsaathi thread.status sync; None if disabled or request failed."""
    if not getattr(settings, "WATI_EXT_MESSAGES_ENABLED", True):
        return None
    data = await fetch_wati_get_messages(phone)
    if not isinstance(data, dict):
        return None
    res = data.get("result")
    if res is not None and str(res).lower() not in ("success", "true", "1"):
        if res is False:
            logger.warning("WATI getMessages unexpected result=%s", res)
            return None
    items = _timeline_items_from_response(data)
    return parse_wati_timeline_for_thread(items)

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


async def send_interactive_buttons_message(phone: str, message: str, action: str) -> bool:
    if not phone:
        logger.warning("Skipping interactive message: phone missing")
        return False

    text = (message or "").strip()
    if not text:
        logger.warning("Skipping interactive message: empty body")
        return False

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
    else:
        logger.warning("Unknown interactive action=%s", action)
        return False

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
            return wati_response_indicates_success(response)
    except Exception:
        logger.exception("Failed sending interactive buttons action=%s", action)
        return False


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
                    {"title": "Apple", "description": "iPhone / iPad"},
                    {"title": "Samsung", "description": "Galaxy"},
                    {"title": "Pixel", "description": "Google Pixel"},
                    {"title": "Oppo", "description": ""},
                    {"title": "Xiaomi", "description": "Redmi / POCO"},
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
