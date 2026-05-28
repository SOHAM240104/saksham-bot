import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request

from wati.services.webhook import (
    append_human_operator_text_to_latest_message,
    process_incoming_message,
    wati_payload_indicates_human_operator,
)

logger = logging.getLogger("wati.router")
router = APIRouter()


async def _handle_incoming(request: Request, background_tasks: BackgroundTasks) -> dict:
    payload = await request.json()

    logger.info(
        "WATI webhook payload:\n%s",
        json.dumps(payload, indent=2, ensure_ascii=False),
    )

    # WATI sets owner=true for outbound messages; ignore for loops unless a human agent sent text.
    if payload.get("owner") is True:
        evt = str(payload.get("eventType") or "").lower()
        text_out = str(payload.get("text") or "").strip()
        phone_out = payload.get("waId")
        msg_type = str(payload.get("type") or "text").lower()
        if (
            phone_out
            and text_out
            and msg_type == "text"
            and wati_payload_indicates_human_operator(payload)
            and (not evt or evt == "sessionmessagesent")
        ):
            background_tasks.add_task(
                append_human_operator_text_to_latest_message,
                str(phone_out),
                text_out,
                payload,
            )
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
    message = ""
    list_reply = payload.get("listReply")
    if isinstance(list_reply, dict):
        message = str(list_reply.get("title") or "").strip()
    if not message:
        message = str(payload.get("text") or "").strip()
    if not message:
        if isinstance(list_reply, dict):
            message = str(list_reply.get("description") or "").strip()
    if not message:
        interactive_btn = payload.get("interactiveButtonReply")
        if isinstance(interactive_btn, dict):
            message = str(interactive_btn.get("title") or "").strip()
    if not message:
        button_reply = payload.get("buttonReply")
        if isinstance(button_reply, dict):
            message = str(button_reply.get("title") or "").strip()
    if not message:
        ticket_raw = str(payload.get("ticketStatus") or payload.get("ticket_status") or "").strip().upper()
        if not ticket_raw and isinstance(payload.get("ticket"), dict):
            tk = payload["ticket"]
            ticket_raw = str(tk.get("status") or tk.get("ticketStatus") or "").strip().upper()
        if ticket_raw == "SOLVED":
            message = "[ticket:SOLVED]"
    if not phone or not message:
        logger.info("Ignoring webhook payload without usable phone/message")
        return {"status": "ignored", "reason": "missing_phone_or_message"}

    logger.info("USER MESSAGE: %s | PHONE: %s", message, phone)

    background_tasks.add_task(process_incoming_message, phone, message, payload)

    return {"status": "ok"}


@router.post("/wati/webhook")
async def wati_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    return await _handle_incoming(request, background_tasks)


