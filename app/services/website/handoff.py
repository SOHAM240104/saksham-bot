"""SSE helpers for human-handoff turns on the website."""

from __future__ import annotations

from typing import Any

from app.schema.messages import MessageResponse


def agent_reply_sse_events(replies: list[MessageResponse]) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for item in replies:
        text = (item.tech_saathi_response or "").strip()
        if not text:
            continue
        events.append(
            (
                "message",
                {
                    "text": text,
                    "role": "techsaathi",
                    "message_id": item.id,
                },
            )
        )
    return events
