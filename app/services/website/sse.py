"""Map chat core turn outcome dicts to website SSE events."""

from __future__ import annotations

import json
from typing import Any, Iterator

from app.services.chat.copy import dynamic_copy

_BUTTON_UI: dict[str, dict[str, Any]] = {
    "mode_buttons": {
        "type": "chips",
        "action": "mode_buttons",
        "options": [
            {"id": "tech", "label": "Tech Help"},
            {"id": "scam", "label": "Scam Help"},
        ],
    },
    "feedback_buttons": {
        "type": "chips",
        "action": "feedback_buttons",
        "options": [
            {"id": "resolved", "label": "Resolved"},
            {"id": "not_resolved", "label": "Still Stuck"},
        ],
    },
    "scam_os_buttons": {
        "type": "chips",
        "action": "scam_os_buttons",
        "options": [
            {"id": "ios", "label": "iPhone / iPad"},
            {"id": "android", "label": "Android"},
        ],
    },
    "branch_clarify_buttons": {
        "type": "chips",
        "action": "branch_clarify_buttons",
        "options": [
            {"id": "continue_tech", "label": "Phone issue"},
            {"id": "scam", "label": "Bank or scam"},
        ],
    },
}

_PLATFORM_PICKER_UI: dict[str, Any] = {
    "type": "platform_picker",
    "action": "platform_buttons",
    "options": [
        {"id": "apple", "label": "Apple", "description": "iPhone / iPad"},
        {"id": "samsung", "label": "Samsung", "description": "Galaxy"},
        {"id": "pixel", "label": "Pixel", "description": "Google Pixel"},
        {"id": "oppo", "label": "Oppo"},
        {"id": "xiaomi", "label": "Xiaomi", "description": "Redmi / POCO"},
    ],
}


def format_sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def outcome_to_sse_events(outcome: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    for item in outcome.get("outbound") or []:
        kind = (item.get("type") or "").strip()

        if kind == "text":
            events.append(("message", {"text": item["text"], "role": "bot"}))
        elif kind == "buttons":
            if item.get("text"):
                events.append(("message", {"text": item["text"], "role": "bot"}))
            ui = _BUTTON_UI.get(item.get("action") or "")
            if ui:
                events.append(("ui", ui))
        elif kind == "platform_picker":
            if item.get("text"):
                events.append(("message", {"text": item["text"], "role": "bot"}))
            events.append(("ui", dict(_PLATFORM_PICKER_UI)))
        elif kind == "handoff":
            events.append(("message", {"text": item["wait_text"], "role": "bot"}))
            events.append(("ui", {"type": "handoff", "status": "waiting"}))
        elif kind == "resolved":
            if item.get("ack_text"):
                events.append(("message", {"text": item["ack_text"], "role": "bot"}))
            if item.get("welcome_text"):
                events.append(("message", {"text": item["welcome_text"], "role": "bot"}))
            events.append(("ui", dict(_BUTTON_UI["mode_buttons"])))
        elif kind == "post_handoff_welcome":
            welcome_text = dynamic_copy("welcome_back_context")
            events.append(("message", {"text": welcome_text, "role": "bot"}))
            events.append(("ui", dict(_BUTTON_UI["mode_buttons"])))

    return events


def iter_sse_stream(events: Iterator[tuple[str, dict[str, Any]]]) -> Iterator[str]:
    for event_name, payload in events:
        yield format_sse_event(event_name, payload)
    yield format_sse_event("status", {"status": "completed"})
