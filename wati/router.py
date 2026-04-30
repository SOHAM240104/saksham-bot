import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request

from wati.services.webhook import process_incoming_message

logger = logging.getLogger("wati.router")
router = APIRouter()


async def _handle_incoming(request: Request, background_tasks: BackgroundTasks) -> dict:
    payload = await request.json()

    logger.info(
        "WATI webhook payload:\n%s",
        json.dumps(payload, indent=2, ensure_ascii=False),
    )

    # WATI sets owner=true for our own outgoing messages; ignore to prevent loops.
    if payload.get("owner") is True:
        logger.info("Ignoring owner=true webhook payload")
        return {"status": "ignored", "reason": "owner_true"}

    event_name = str(payload.get("eventType") or "").lower()
    allowed_events = {"message", "messagereceived", "incomingmessage"}
    if event_name and (
        event_name not in allowed_events
        and "received" not in event_name
        and "incoming" not in event_name
    ):
        logger.info("Ignoring webhook event: %s", event_name)
        return {"status": "ignored", "event": event_name}

    phone = payload.get("waId")
    message = payload.get("text") or ""
    if not phone or not str(message).strip():
        logger.info("Ignoring webhook payload without usable phone/message")
        return {"status": "ignored", "reason": "missing_phone_or_message"}

    logger.info("USER MESSAGE: %s | PHONE: %s", message, phone)

    background_tasks.add_task(process_incoming_message, phone, message, payload)

    return {"status": "ok"}


@router.post("/wati/webhook")
async def wati_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    return await _handle_incoming(request, background_tasks)


