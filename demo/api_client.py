"""Thin HTTP client for the Saksham FastAPI backend — SSE parser + REST helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field

import requests


@dataclass
class SSEEvent:
    event: str = "message"
    data: dict = field(default_factory=dict)


def iter_sse_events(response: requests.Response) -> Iterator[SSEEvent]:
    """Yield SSE events as soon as each data line arrives on the wire."""
    current_event = "message"
    for line in response.iter_lines(decode_unicode=True):
        if line is None:
            continue
        if line == "":
            continue
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ").strip()
        elif line.startswith("data: "):
            raw = line.removeprefix("data: ").strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"raw": raw}
            yield SSEEvent(event=current_event, data=data)
            current_event = "message"


def parse_sse_stream(response: requests.Response) -> list[SSEEvent]:
    """Parse a text/event-stream response into a list of SSEEvent objects."""
    return list(iter_sse_events(response))


class SakshamClient:
    """Wraps all Saksham FastAPI endpoints used by the UAT console."""

    def __init__(self, base_url: str, jwt_token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.jwt_token = jwt_token

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.jwt_token:
            h["Authorization"] = f"Bearer {self.jwt_token}"
        return h

    # ── Senior chat ──────────────────────────────────────────────

    def stream_message(
        self,
        message: str,
        selection_id: str | None = None,
    ) -> Iterator[SSEEvent]:
        """POST /chat/message and yield SSE events incrementally."""
        body: dict = {"message": message}
        if selection_id:
            body["selection_id"] = selection_id
        resp = requests.post(
            f"{self.base_url}/chat/message",
            json=body,
            headers=self._headers(),
            stream=True,
            timeout=120,
        )
        resp.raise_for_status()
        yield from iter_sse_events(resp)

    def send_message(self, message: str, selection_id: str | None = None) -> list[SSEEvent]:
        return list(self.stream_message(message, selection_id))

    def get_messages(self, since_id: int | None = None) -> list[dict]:
        params: dict = {}
        if since_id is not None:
            params["since_id"] = since_id
        resp = requests.get(
            f"{self.base_url}/chat/messages",
            params=params,
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    # ── Agent inbox ──────────────────────────────────────────────

    def list_conversations(self) -> list[dict]:
        resp = requests.get(
            f"{self.base_url}/agent/conversations",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_conversation(self, conversation_id: int) -> dict:
        resp = requests.get(
            f"{self.base_url}/agent/conversations/{conversation_id}",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def get_conversation_messages(self, conversation_id: int) -> list[dict]:
        resp = requests.get(
            f"{self.base_url}/agent/conversations/{conversation_id}/messages",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def send_agent_reply(self, conversation_id: int, message: str) -> dict:
        resp = requests.post(
            f"{self.base_url}/agent/conversations/{conversation_id}/messages",
            json={"message": message},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def resolve_conversation(self, conversation_id: int) -> dict:
        resp = requests.post(
            f"{self.base_url}/agent/conversations/{conversation_id}/resolve",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})
