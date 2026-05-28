"""Code-owned Still Stuck state machine: diagnostic → refined retry → handoff."""

import logging

from wati.llm.rag_chain import search_support_docs

logger = logging.getLogger("wati.services.unresolved_flow")

UNRESOLVED_PHASE_NONE = "none"
UNRESOLVED_PHASE_DIAGNOSTIC = "diagnostic"
UNRESOLVED_PHASE_REFINED_RETRY = "refined_retry"


def _looks_like_unresolved_diagnostic(text: str) -> bool:
    """True when assistant text repeats the Still Stuck diagnostic question."""
    if not text:
        return False
    lowered = text.lower()
    markers = (
        "step didn't work",
        "step didn’t work",
        "which step didn't",
        "which step didn",
        "what did you see instead",
        "what you saw instead",
        "share your ios version",
        "share your ipados version",
        "share your coloros version",
        "share your hyperos version",
        "share your galaxy model",
    )
    return any(marker in lowered for marker in markers)


def _last_bot_was_troubleshooting_steps(
    last_bot_message_source: str,
    last_bot_response: str,
) -> bool:
    source = (last_bot_message_source or "").lower()
    if "feedback_buttons" in source or "refined_unresolved" in source:
        return True
    from wati.services.conversation import _looks_like_troubleshooting_steps

    return bool(last_bot_response and _looks_like_troubleshooting_steps(last_bot_response))


def _last_bot_was_diagnostic(
    last_bot_message_source: str,
    last_bot_response: str,
) -> bool:
    source = (last_bot_message_source or "").lower()
    if "unresolved_diagnostic" in source:
        return True
    return bool(last_bot_response and _looks_like_unresolved_diagnostic(last_bot_response))


def resolve_unresolved_phase(
    *,
    on_tech_path: bool,
    scam_mode: bool,
    same_issue_as_previous: bool,
    button_reply_id: str,
    current_message: str,
    last_bot_message_source: str,
    last_bot_response: str,
) -> str:
    """Deterministic Still Stuck phase for this turn."""
    from wati.services.conversation import _is_unresolved_negative_only

    if scam_mode or not on_tech_path:
        return UNRESOLVED_PHASE_NONE
    if not same_issue_as_previous:
        return UNRESOLVED_PHASE_NONE

    last_source = (last_bot_message_source or "").strip()
    last_text = (last_bot_response or "").strip()

    if _is_unresolved_negative_only(current_message, button_reply_id):
        if _last_bot_was_troubleshooting_steps(last_source, last_text):
            return UNRESOLVED_PHASE_DIAGNOSTIC
        return UNRESOLVED_PHASE_NONE

    if _last_bot_was_diagnostic(last_source, last_text):
        return UNRESOLVED_PHASE_REFINED_RETRY

    return UNRESOLVED_PHASE_NONE


def retrieve_for_refined_unresolved_turn(
    *,
    history: list[dict],
    current_message: str,
    pending_user_issue: str,
    platform: str,
    use_platform_only_retrieval: bool = False,
) -> dict:
    """
    Run deterministic RAG retrieval for refined Still Stuck retry.

    On success returns: {"context": str, "sources": list[str], "confidence_score": float|None}
    On failure returns a generate_wati_reply-shaped dict with "kind".
    """
    from wati.services.conversation import (
        _infer_runtime_context,
        _last_assistant_troubleshooting_from_history,
        _map_retrieval_os_and_version,
        _platform_label,
    )

    platform_slug = (platform or "").strip().lower()
    if not platform_slug:
        return {
            "kind": "action",
            "action": "platform_buttons",
            "message": "Which phone are you using?",
            "message_source": "refined_unresolved",
        }

    chat_ctx = _infer_runtime_context(history)
    known_os_version = (chat_ctx.get("known_os_version") or "").strip().lower()
    known_model = (chat_ctx.get("known_model") or "").strip().lower()
    if use_platform_only_retrieval:
        known_os_version = ""
        known_model = ""

    retrieval_os, retrieval_version = _map_retrieval_os_and_version(
        platform_slug, known_os_version, known_model
    )
    base_query = (pending_user_issue or current_message or "").strip()
    prior_steps = _last_assistant_troubleshooting_from_history(history)
    if prior_steps:
        base_query = (
            f"{base_query}\n"
            "User already tried the steps below without success — "
            "need a DIFFERENT approach (alternate settings path or diagnostic):\n"
            f"{prior_steps[:700]}"
        )
    hint_lines = []
    if retrieval_os:
        hint_lines.append(f"os_family: {retrieval_os}")
    if retrieval_version:
        hint_lines.append(f"os_version: {retrieval_version}")
    if known_model and platform_slug == "samsung":
        hint_lines.append(f"device_model: {known_model}")
    if (current_message or "").strip() and current_message.strip() != pending_user_issue:
        hint_lines.append(f"user_refinement: {(current_message or '').strip()}")
    if hint_lines:
        base_query = f"{base_query}\n" + "\n".join(hint_lines)

    os_version_arg = retrieval_version or ""
    try:
        result = search_support_docs.invoke(
            {
                "user_query": base_query,
                "platform": platform_slug,
                "os_version": os_version_arg,
                "retrieval_mode": "retry",
            }
        )
    except Exception:
        logger.exception("refined_unresolved search_support_docs failed")
        return {
            "kind": "text",
            "message": (
                f"I had trouble loading guides for your {_platform_label(platform_slug)}. "
                "Please try describing the issue again."
            ),
            "message_source": "refined_unresolved,error",
        }

    kb_context = (result.get("context") or "").strip()
    sources = result.get("sources") or []
    confidence = result.get("confidence_score")
    if not isinstance(confidence, (int, float)):
        confidence = None

    if not kb_context:
        return {
            "kind": "text",
            "message": (
                f"I couldn't find updated steps for that on your {_platform_label(platform_slug)} right now. "
                "Can you tell me what you see on screen when you try again?"
            ),
            "message_source": "refined_unresolved,empty_kb",
        }

    source_tags = [str(s) for s in sources if str(s).strip()]
    return {
        "context": kb_context,
        "sources": source_tags,
        "confidence_score": confidence,
    }
