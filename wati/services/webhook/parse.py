import re


def wati_payload_indicates_human_operator(payload: dict | None) -> bool:
    p = payload or {}
    if (p.get("operatorName") or "").strip():
        return True
    e = (p.get("operatorEmail") or "").strip().lower()
    return bool(e) and "api-token-user" not in e


def _extract_name(payload: dict) -> str | None:
    for key in ["senderName", "contactName", "name", "profileName"]:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:512]
    return None


def _normalize_support_button_id(
    reply_id: str | None, title: str | None = None
) -> str | None:
    """Map WATI quick-reply ids (often 1/2) and titles to tech/scam."""
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
    }:
        return normalized
    return normalized


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


def _timeline_items_from_response(data: dict) -> list:
    """v1 getMessages uses messages.items; ext v3 used message_list."""
    if not isinstance(data, dict):
        return []
    msgs = data.get("messages")
    if isinstance(msgs, dict):
        items = msgs.get("items")
        if isinstance(items, list):
            return items
    raw = data.get("message_list")
    return raw if isinstance(raw, list) else []


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


def parse_wati_timeline_for_thread(items: list) -> str:
    if not isinstance(items, list):
        return "assigned"

    ts_human_assigned = ""
    ts_closed = ""
    ts_bot_reopened = ""

    for it in items:
        if not isinstance(it, dict):
            continue

        event_type, desc_raw = _timeline_item_fields(it)
        if event_type != "ticket":
            continue

        desc = desc_raw.lower()
        ts = str(it.get("created") or "")

        # human took control
        if "assigned to" in desc and "bot" not in desc:
            if ts > ts_human_assigned:
                ts_human_assigned = ts

        # human closed
        if "chat has been closed" in desc or "closed by agent" in desc:
            if ts > ts_closed:
                ts_closed = ts

        #  bot took back control
        if "ticket status" in desc and "open" in desc and "bot" in desc:
            if ts > ts_bot_reopened:
                ts_bot_reopened = ts

        if "chat has been initialized" in desc:
            if ts > ts_bot_reopened:
                ts_bot_reopened = ts

    latest_end = max(ts_closed, ts_bot_reopened)

    if latest_end and latest_end >= ts_human_assigned:
        return "resolved"

    return "assigned"
