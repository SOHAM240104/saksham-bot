import re
from datetime import UTC, datetime

# This function is used to check if the payload indicates that a human operator is involved in the conversation.
def wati_payload_indicates_human_operator(payload: dict | None) -> bool:
    p = payload or {}
    if (p.get("operatorName") or "").strip():
        return True
    e = (p.get("operatorEmail") or "").strip().lower()
    return bool(e) and "api-token-user" not in e  # This is a check to ensure that the operator email is not a bot email. another thing we can do is if the owner is true then we can return true.

# This function is used to extract the name from the payload.
def _extract_name(payload: dict) -> str | None:
    for key in ["senderName", "contactName", "name", "profileName"]:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:512]
    return None

# This function is used to normalize the scam OS button.
def _normalize_scam_os_button(title: str | None = None, reply_id: str | None = None) -> str | None:
    """Map only explicit Scam Help OS button taps — never free-text keywords."""
    if isinstance(reply_id, str) and reply_id.strip():
        normalized = reply_id.strip().lower()
        if normalized in {"ios", "android"}:
            return normalized
    if isinstance(title, str) and title.strip():
        if title.strip() == "iPhone / iPad":
            return "ios"
        if title.strip() == "Android":
            return "android"
    return None

# This function is used to normalize the support button id.
def _normalize_support_button_id(
    reply_id: str | None, title: str | None = None
) -> str | None:
    """Map WATI quick-reply ids (often 1/2) and titles to tech/scam."""
    os_mapped = _normalize_scam_os_button(title, reply_id)
    if os_mapped:
        return os_mapped
    if isinstance(title, str) and title.strip():
        normalized_title = title.strip().lower()
        if "tech help" in normalized_title:
            return "tech"
        if "scam help" in normalized_title:
            return "scam"
    if not isinstance(reply_id, str) or not reply_id.strip():
        return None
    normalized = reply_id.strip().lower()
    if normalized in {"1", "tech"}:
        return "tech"
    if normalized in {"2", "scam"}:
        return "scam"
    if normalized in {
        "resolved",
        "not_resolved",
        "tech",
        "scam",
        "ios",
        "android",
    }:
        return normalized
    return normalized

# This function is used to extract the button reply id from the payload.
def _extract_button_reply_id(payload: dict | None) -> str | None:
    raw = payload or {}

    list_reply = raw.get("listReply")
    if isinstance(list_reply, dict):
        title = list_reply.get("title")
        if isinstance(title, str) and title.strip():
            normalized_title = title.strip().lower()
            if normalized_title in {"apple", "samsung", "pixel", "oppo", "xiaomi"}:
                return normalized_title
        reply_id = list_reply.get("id")
        if isinstance(reply_id, str) and reply_id.strip():
            normalized = reply_id.strip().lower()
            if normalized in {"apple", "samsung", "pixel", "oppo", "xiaomi"}:
                return normalized

    interactive_button_reply = raw.get("interactiveButtonReply")
    if isinstance(interactive_button_reply, dict):
        title = interactive_button_reply.get("title")
        if isinstance(title, str) and title.strip():
            normalized_title = title.strip().lower()
            if normalized_title in {"apple", "samsung", "pixel", "oppo", "xiaomi"}:
                return normalized_title
            os_mapped = _normalize_scam_os_button(title, interactive_button_reply.get("id"))
            if os_mapped:
                return os_mapped
            if "resolved" in normalized_title and "stuck" not in normalized_title:
                return "resolved"
            if "stuck" in normalized_title or "not resolved" in normalized_title:
                return "not_resolved"
        mapped = _normalize_support_button_id(
            interactive_button_reply.get("id"),
            title if isinstance(title, str) else None,
        )
        if mapped:
            return mapped

    button_reply = raw.get("buttonReply")
    if isinstance(button_reply, dict):
        title = button_reply.get("title")
        if isinstance(title, str) and title.strip():
            normalized_title = title.strip().lower()
            if normalized_title in {"apple", "samsung", "pixel", "oppo", "xiaomi"}:
                return normalized_title
            os_mapped = _normalize_scam_os_button(title, button_reply.get("id"))
            if os_mapped:
                return os_mapped
            if "resolved" in normalized_title and "stuck" not in normalized_title:
                return "resolved"
            if "stuck" in normalized_title or "not resolved" in normalized_title:
                return "not_resolved"
        mapped = _normalize_support_button_id(
            button_reply.get("id"),
            title if isinstance(title, str) else None,
        )
        if mapped:
            return mapped

    interactive_data = raw.get("interactiveData")
    if isinstance(interactive_data, dict):
        button_id = interactive_data.get("buttonId")
        mapped = _normalize_support_button_id(
            button_id if isinstance(button_id, str) else None,
            None,
        )
        if mapped:
            return mapped

    return None


def _is_handoff_confirmation_message(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not t:
        return False
    has_confirm = "yes or no" in t or "reply yes" in t or "please reply yes" in t
    has_human = any(
        p in t
        for p in (
            "human agent",
            "human support",
            "support agent",
            "connect you",
            "connect me",
            "tech saathi",
        )
    )
    return has_confirm and has_human


def _handoff_confirm_decision(text: str) -> str:
    msg = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    yes_tokens = {"yes", "y", "haan", "ha", "ok", "okay", "sure", "connect", "transfer"}
    no_tokens = {"no", "n", "nah", "nope", "not now", "dont", "don't", "later"}
    if any(tok in msg for tok in yes_tokens):
        return "YES"
    if any(tok in msg for tok in no_tokens):
        return "NO"
    return "UNCLEAR"

# This function is used to extract the timeline items from the response.
def _timeline_items_from_response(data: dict) -> list:
    """v1 getMessages uses messages.items; """
    if not isinstance(data, dict):
        return []
    msgs = data.get("messages") # This is the messages from the response.
    if isinstance(msgs, dict):
        items = msgs.get("items") # This is the items from the response.
        if isinstance(items, list):
            return items
    raw = data.get("message_list") # This is the message list from the response.
    return raw if isinstance(raw, list) else []




# This function is used to parse the WATI created timestamp to UTC datetime for reliable comparisons.
def _parse_wati_created(raw) -> datetime | None:
    """ WATI ``created`` to UTC datetime for reliable comparisons."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=UTC) # This is the timestamp from the response.
        except (OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00" # This is the formatted timestamp from the response.
    try:
        parsed = datetime.fromisoformat(text) 
    except ValueError:
        return None
    if parsed.tzinfo is None: 
        return parsed.replace(tzinfo=UTC) 
    return parsed.astimezone(UTC) #.astimezone(UTC) so all comparisons use one zone.


def _timeline_ticket_id(item: dict) -> str:
    """Ticket id groups timeline events — one phone can have many tickets over time."""
    for key in ("ticketId", "ticket_id"):
        val = item.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _timeline_item_fields(item: dict) -> tuple[str, str]:
    """Return (event_type_lower, combined_description) for WATI timeline items."""
    event_type = str(item.get("eventType") or item.get("event_type") or "").lower()
    ev_desc = str(item.get("eventDescription") or item.get("event_description") or "")
    det = item.get("detailedEventDescription") or item.get("detailed_event_description")
    detail_parts: list[str] = []
    if isinstance(det, dict):
        for key in ("agentName", "status", "triggerSourceName", "flowName"):
            v = det.get(key)
            if isinstance(v, str) and v.strip():
                detail_parts.append(v.strip())
    detailed = " ".join(detail_parts)
    combined = " ".join(p for p in (ev_desc, detailed) if p).strip()
    return event_type, combined

# This function is used to check if the event is a human assign event.
def _is_human_assign_event(desc: str) -> bool:
    """True when a human agent (not the bot) took ownership of the chat."""
    if "chat is now assigned to" in desc and "@" in desc: #"chat is now assigned to" and "@" (agent email) → True.
        return True
    return (
        "assigned to" in desc
        and "assigned to bot" not in desc
        and "assigned back to bot" not in desc
    )


def _is_close_event(desc: str) -> bool:
    """True when the agent closed or marked the ticket solved in WATI."""
    return (
        "chat has been closed" in desc
        or "closed by agent" in desc
        or "status changed to solved" in desc
        or ("ticket status" in desc and "solved" in desc)
    )


def _is_bot_reopen_event(desc: str) -> bool:
    """True when control returned to the bot (new session / ticket reopened to bot)."""
    if "ticket status" in desc and "open" in desc and "bot" in desc:
        return True
    return "chat has been initialized" in desc

#Find the most recent close/reopen across all tickets in the snippet.
def _latest_close_and_bot_reopen(
    ticket_events: list[dict],
) -> tuple[tuple[datetime, str] | None, tuple[datetime, str] | None]:
    """Scan all ticket events for the most recent close and bot-reopen (with ticket id)."""
    latest_close: tuple[datetime, str] | None = None
    latest_bot_reopen: tuple[datetime, str] | None = None
    for event in ticket_events:
        if _is_close_event(event["desc"]):
            if latest_close is None or event["created"] > latest_close[0]:
                latest_close = (event["created"], event["ticket_id"])
        if _is_bot_reopen_event(event["desc"]):
            if latest_bot_reopen is None or event["created"] > latest_bot_reopen[0]:
                latest_bot_reopen = (event["created"], event["ticket_id"])
    return latest_close, latest_bot_reopen


def parse_wati_timeline_for_thread(items: list) -> str:
    """Decide if the human handoff is still active or finished."""
    if not isinstance(items, list):
        return "assigned"

    ticket_events: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue

        event_type, desc_raw = _timeline_item_fields(it)
        if event_type != "ticket":
            continue

        created = _parse_wati_created(it.get("created"))
        if created is None:
            continue

        ticket_events.append(
            {
                "created": created,
                "ticket_id": _timeline_ticket_id(it),
                "desc": desc_raw.lower(),
            }
        )

    latest_assign: tuple[datetime, str] | None = None
    for event in ticket_events:
        if not _is_human_assign_event(event["desc"]):
            continue
        if latest_assign is None or event["created"] > latest_assign[0]:
            latest_assign = (event["created"], event["ticket_id"])

    # Assign event scrolled out of the 5-item window — infer from close/reopen only.
    if latest_assign is None:
        latest_close, latest_bot_reopen = _latest_close_and_bot_reopen(ticket_events)
        if latest_close:
            close_ts, _close_tid = latest_close
            if latest_bot_reopen and latest_bot_reopen[0] > close_ts:
                # Agent closed; user/bot started a session after close (often a new ticket).
                return "resolved"
            return "resolved"
        return "assigned"

    assign_ts, active_ticket_id = latest_assign
    latest_close_on_ticket: datetime | None = None
    latest_bot_reopen_after_assign: datetime | None = None
    global_close, global_bot_reopen = _latest_close_and_bot_reopen(ticket_events)

    # Only consider close/reopen on the ticket the human was assigned to.
    for event in ticket_events:
        if event["ticket_id"] != active_ticket_id:
            continue
        if _is_close_event(event["desc"]):
            if latest_close_on_ticket is None or event["created"] > latest_close_on_ticket:
                latest_close_on_ticket = event["created"]
        if _is_bot_reopen_event(event["desc"]) and event["created"] >= assign_ts:
            if (
                latest_bot_reopen_after_assign is None
                or event["created"] > latest_bot_reopen_after_assign
            ):
                latest_bot_reopen_after_assign = event["created"]

    if latest_close_on_ticket and latest_close_on_ticket >= assign_ts:
        return "resolved"

    # Close on handoff ticket + bot session on a new ticket → handoff is done.
    if (
        global_close
        and global_close[0] >= assign_ts
        and global_close[1] == active_ticket_id
        and global_bot_reopen
        and global_bot_reopen[0] > global_close[0]
        and global_bot_reopen[1] != global_close[1]
    ):
        return "resolved"

    if latest_close_on_ticket is None and latest_bot_reopen_after_assign:
        # Bot reopened on same ticket but no close yet — treat as still with human.
        return "assigned"

    return "assigned"
