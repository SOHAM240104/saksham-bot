"""What the bot wants to send — independent of SSE, WebSocket, or HTTP."""

from __future__ import annotations

from typing import Any

# TurnOutcome and each outbound item are plain dicts with a "type" key.
# Keeps the brain transport-agnostic without dataclasses.


def turn_outcome(
    *,
    outbound: list[dict[str, Any]] | None = None,
    thread_id: int | None = None,
    bot_response: str | None = None,
    message_source: str | None = None,
    confidence_score: float | None = None,
    patch_message_source_only: bool = False,
) -> dict[str, Any]:
    """One turn result: what to show (outbound) + what to save on the message row."""
    return {
        "outbound": list(outbound or []),
        "thread_id": thread_id,
        "bot_response": bot_response,
        "message_source": message_source,
        "confidence_score": confidence_score,
        "patch_message_source_only": patch_message_source_only,
    }


def should_save_bot_reply(outcome: dict[str, Any]) -> bool:
    """True when chat router should call save_bot_reply for this turn."""
    return (
        outcome.get("bot_response") is not None
        or outcome.get("message_source") is not None
        or outcome.get("confidence_score") is not None
        or bool(outcome.get("patch_message_source_only"))
    )


def needs_outcome_delivery(outcome: dict[str, Any]) -> bool:
    """True when there is something to stream and/or persist."""
    return bool(outcome.get("outbound")) or should_save_bot_reply(outcome)


# --- outbound item factories (each dict is one kind of UI/text the bot might send) ---


def text_outbound(
    text: str,
    *,
    message_source: str = "llm",
    confidence_score: float | None = None,
) -> dict[str, Any]:
    # plain bot message — website effect: SSE message event
    return {
        "type": "text",
        "text": text,
        "message_source": message_source,
        "confidence_score": confidence_score,
    }


def buttons_outbound(
    text: str,
    action: str,
    *,
    message_source: str = "",
    confidence_score: float | None = None,
) -> dict[str, Any]:
    # text + chip row (Tech/Scam, Resolved/Still Stuck, iOS/Android)
    # website effect: SSE message + ui chips
    return {
        "type": "buttons",
        "text": text,
        "action": action,
        "message_source": message_source,
        "confidence_score": confidence_score,
    }


def platform_picker_outbound(
    text: str,
    *,
    message_source: str = "platform_list",
    confidence_score: float | None = None,
) -> dict[str, Any]:
    # phone brand picker — website effect: SSE message + platform_picker ui
    return {
        "type": "platform_picker",
        "text": text,
        "message_source": message_source,
        "confidence_score": confidence_score,
    }


def handoff_outbound(
    wait_text: str,
    *,
    message_source: str = "handoff",
    handoff_count: int = 1,
) -> dict[str, Any]:
    # connecting to human agent — website effect: SSE message + handoff waiting ui
    return {
        "type": "handoff",
        "wait_text": wait_text,
        "message_source": message_source,
        "handoff_count": handoff_count,
    }


def resolved_outbound(
    ack_text: str,
    welcome_text: str,
    *,
    message_source: str = "conversation_control,post_resolve_welcome,mode_buttons",
    new_thread_id: int | None = None,
) -> dict[str, Any]:
    # glad that helped + welcome + mode chips — website effect: messages + mode chips ui
    return {
        "type": "resolved",
        "ack_text": ack_text,
        "welcome_text": welcome_text,
        "message_source": message_source,
        "new_thread_id": new_thread_id,
    }


def post_handoff_welcome_outbound(
    conversation_id: int,
    new_thread_id: int,
) -> dict[str, Any]:
    # welcome after agent resolve — website effect: message + mode chips ui
    return {
        "type": "post_handoff_welcome",
        "conversation_id": conversation_id,
        "new_thread_id": new_thread_id,
    }


def default_button_caption(action: str, *, feedback_checkin: str) -> str:
    if action == "mode_buttons":
        return "How can I help you today?"
    if action == "platform_buttons":
        return "Which phone are you using?"
    if action == "feedback_buttons":
        return feedback_checkin
    if action == "scam_os_buttons":
        return "To guide you better, is your phone an iPhone/iPad or Android?"
    if action == "branch_clarify_buttons":
        return "Did you mean your phone issue, or something about your bank or a possible scam?"
    return "Please choose an option."


def outcome_from_llm_reply(
    llm_result: dict,
    *,
    support_mode: str,
    scam_context: bool,
    feedback_checkin: str,
    resolved_ack: str,
    welcome_text: str,
    new_thread_id: int | None,
    episode=None,
) -> dict[str, Any]:
    """Convert LLM tool/text reply dict into outbound items + DB fields."""
    from app.services.chat.episode import (
        episode_after_outbound,
        empty_episode,
        tag_message_source,
    )

    prior_ep = episode if episode is not None else empty_episode()
    response_source = (llm_result.get("message_source") or "").strip()
    response_confidence = llm_result.get("confidence_score")
    if not isinstance(response_confidence, (int, float)):
        response_confidence = None

    def _tag(src: str, *, action: str = "", kind: str = "") -> str:
        next_ep = episode_after_outbound(
            prior=prior_ep,
            message_source=src,
            action=action,
            kind=kind,
            forced_branch=(
                "scam"
                if scam_context
                else ("tech" if (support_mode or "").strip().lower() == "tech" else None)
            ),
        )
        return tag_message_source(src, next_ep)

    if llm_result.get("kind") == "control":
        control_action = (llm_result.get("action") or "").strip().lower()
        if control_action == "resolved":
            src = _tag(
                "conversation_control,post_resolve_welcome,mode_buttons",
                kind="resolved",
            )
            return turn_outcome(
                outbound=[
                    resolved_outbound(
                        ack_text=resolved_ack,
                        welcome_text=welcome_text,
                        message_source=src,
                        new_thread_id=new_thread_id,
                    )
                ],
                bot_response=f"{resolved_ack}\n\n---\n\n{welcome_text}",
                message_source=src,
                confidence_score=response_confidence,
            )
        return turn_outcome(
            bot_response="",
            message_source=_tag("conversation_control"),
        )

    if llm_result.get("kind") == "multi_text":
        parts = llm_result.get("messages") or []
        outbound: list[dict[str, Any]] = []
        bot_chunks: list[str] = []
        part_source = response_source
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = (part.get("message") or "").strip()
            if not text:
                continue
            src = (part.get("message_source") or "").strip() or "llm"
            if src and not part_source:
                part_source = src
            tagged = _tag(src)
            outbound.append(
                text_outbound(text, message_source=tagged, confidence_score=response_confidence)
            )
            bot_chunks.append(text)
        return turn_outcome(
            outbound=outbound,
            bot_response="\n\n---\n\n".join(bot_chunks),
            message_source=_tag(part_source or "llm"),
            confidence_score=response_confidence,
        )

    if llm_result.get("kind") == "action":
        action = llm_result.get("action")
        if action == "scam_os_buttons" and (
            (support_mode or "").strip().lower() == "tech" or not scam_context
        ):
            action_message = (
                (llm_result.get("message") or "").strip()
                or default_button_caption("platform_buttons", feedback_checkin=feedback_checkin)
            )
            src = _tag(response_source or "platform_list", action="platform_buttons")
            return turn_outcome(
                outbound=[
                    platform_picker_outbound(
                        action_message,
                        message_source=src,
                        confidence_score=response_confidence,
                    )
                ],
                bot_response=action_message,
                message_source=src,
                confidence_score=response_confidence,
            )

        action_message = (llm_result.get("message") or "").strip() or default_button_caption(
            action or "", feedback_checkin=feedback_checkin
        )
        if action == "platform_buttons":
            src = _tag(response_source or "platform_list", action="platform_buttons")
            return turn_outcome(
                outbound=[
                    platform_picker_outbound(
                        action_message,
                        message_source=src,
                        confidence_score=response_confidence,
                    )
                ],
                bot_response=action_message,
                message_source=src,
                confidence_score=response_confidence,
            )
        src = _tag(response_source or "llm", action=action or "")
        return turn_outcome(
            outbound=[
                buttons_outbound(
                    action_message,
                    action or "",
                    message_source=src,
                    confidence_score=response_confidence,
                )
            ],
            bot_response=action_message,
            message_source=src,
            confidence_score=response_confidence,
        )

    bot_response = (llm_result.get("message") or "").strip()
    src = _tag(response_source or "llm")
    return turn_outcome(
        outbound=[
            text_outbound(
                bot_response,
                message_source=src,
                confidence_score=response_confidence,
            )
        ],
        bot_response=bot_response,
        message_source=src,
        confidence_score=response_confidence,
    )
