"""Code-owned Still Stuck state machine: diagnostic → refined retry → handoff."""

import logging

from app.services.chat.rag import search_support_docs

logger = logging.getLogger("app.services.chat.still_stuck")

UNRESOLVED_PHASE_NONE = "none"
UNRESOLVED_PHASE_DIAGNOSTIC = "diagnostic"
UNRESOLVED_PHASE_REFINED_RETRY = "refined_retry"


def _prior_steps_match_issue(prior_steps: str, issue: str) -> bool:
    """True when last troubleshooting text looks related to the current issue."""
    from app.services.chat.reply import _normalize_issue_signature

    issue_tokens = {
        t for t in _normalize_issue_signature(issue).split("_") if len(t) > 2
    }
    steps_tokens = {
        t
        for t in _normalize_issue_signature((prior_steps or "")[:500]).split("_")
        if len(t) > 2
    }
    if not issue_tokens:
        return True
    if issue_tokens & steps_tokens:
        # Still reject hearing-aid paths for normal speaker/headphones issues
        issue_l = (issue or "").lower()
        steps_l = (prior_steps or "").lower()
        hearing_user = any(
            k in issue_l for k in ("hearing aid", "hearing aids", "hearing device")
        )
        hearing_steps = any(
            k in steps_l
            for k in ("hearing device", "hearing aids", "mfi hearing", "mfi ")
        )
        bluetooth_user = any(
            k in issue_l
            for k in ("bluetooth", "speaker", "headphone", "headphones", "earbuds", "earbud")
        )
        if bluetooth_user and hearing_steps and not hearing_user:
            return False
        return True
    # Domain clash: wifi issue vs bluetooth/headphones steps, etc.
    domains = {
        "wifi": {"wifi", "wi", "disconnect", "disconnecting", "network", "router"},
        "bluetooth": {
            "bluetooth",
            "speaker",
            "headphone",
            "headphones",
            "earbuds",
            "airpod",
        },
        "hearing": {"hearing", "mfi", "aid", "aids"},
        "battery": {"battery", "drain", "drains", "charging", "charge", "hot", "heat"},
        "screen": {"screen", "flicker", "flickering", "display", "brightness"},
        "photos": {"photo", "photos", "gallery", "album", "albums"},
        "apps": {"app", "apps", "update", "store"},
        "alarm": {"alarm", "volume", "sound", "ringtone"},
        "flashlight": {"flashlight", "torch", "flash"},
    }
    issue_domains = {
        name
        for name, keys in domains.items()
        if any(k in (issue or "").lower() for k in keys)
    }
    steps_domains = {
        name
        for name, keys in domains.items()
        if any(k in (prior_steps or "").lower() for k in keys)
    }
    if issue_domains and steps_domains:
        if issue_domains.isdisjoint(steps_domains):
            return False
        # Bluetooth speaker issue must not match hearing-aid-only steps
        if "bluetooth" in issue_domains and "hearing" in steps_domains:
            if "hearing" not in issue_domains:
                return False
        return True
    return False


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
        "what happened when you tried",
        "sorry it's still stuck",
        "sorry it’s still stuck",
        "share your ios version",
        "share your ipados version",
        "share your coloros version",
        "share your hyperos version",
        "share your galaxy model",
        "if you know your",
    )
    return any(marker in lowered for marker in markers)


def _last_bot_was_troubleshooting_steps(
    last_bot_message_source: str,
    last_bot_response: str,
) -> bool:
    source = (last_bot_message_source or "").lower()
    if "feedback_buttons" in source or "refined_unresolved" in source:
        return True
    from app.services.chat.reply import _looks_like_troubleshooting_steps

    return bool(last_bot_response and _looks_like_troubleshooting_steps(last_bot_response))


def _last_bot_was_diagnostic(
    last_bot_message_source: str,
    last_bot_response: str,
) -> bool:
    source = (last_bot_message_source or "").lower()
    if "unresolved_diagnostic" in source:
        return True
    return bool(last_bot_response and _looks_like_unresolved_diagnostic(last_bot_response))


def _last_bot_was_refined_unresolved_steps(last_bot_message_source: str) -> bool:
    return "refined_unresolved" in (last_bot_message_source or "").lower()


def resolve_unresolved_phase(
    *,
    on_tech_path: bool,
    scam_mode: bool,
    same_issue_as_previous: bool,
    button_reply_id: str,
    current_message: str,
    last_bot_message_source: str,
    last_bot_response: str,
    episode_action: str = "",
) -> str:
    """Deterministic Still Stuck phase for this turn.

    Prefer episode FSM action when provided; fall back to legacy source tags.
    """
    from app.services.chat.reply import is_unresolved_negative_only

    if scam_mode or not on_tech_path:
        return UNRESOLVED_PHASE_NONE

    action = (episode_action or "").strip().lower()
    if action == "diagnostic":
        return UNRESOLVED_PHASE_DIAGNOSTIC
    if action == "refined":
        return UNRESOLVED_PHASE_REFINED_RETRY
    if action == "handoff":
        return UNRESOLVED_PHASE_NONE

    if not same_issue_as_previous:
        return UNRESOLVED_PHASE_NONE

    last_source = (last_bot_message_source or "").strip()
    last_text = (last_bot_response or "").strip()

    if is_unresolved_negative_only(current_message, button_reply_id):
        if _last_bot_was_refined_unresolved_steps(last_source):
            # Cycle 3 complete — handoff is handled in code; do not re-ask diagnostic.
            return UNRESOLVED_PHASE_NONE
        if _last_bot_was_troubleshooting_steps(last_source, last_text):
            return UNRESOLVED_PHASE_DIAGNOSTIC
        return UNRESOLVED_PHASE_NONE

    if _last_bot_was_diagnostic(last_source, last_text):
        return UNRESOLVED_PHASE_REFINED_RETRY

    return UNRESOLVED_PHASE_NONE


def should_auto_handoff_by_unresolved_depth(
    *,
    scam_mode: bool,
    on_tech_path: bool,
    same_issue_as_previous: bool,
    button_reply_id: str,
    current_message: str,
    issue_followup_depth: int,
    unresolved_phase: str,
    last_bot_message_source: str = "",
    episode_handoff: bool = False,
) -> bool:
    """
    Auto-escalate on Still Stuck after refined steps, or when episode FSM
    says handoff (e.g. stuck loop asking for OS without ever sending steps).
    """
    from app.services.chat.reply import is_unresolved_negative_only

    if scam_mode or not on_tech_path:
        return False

    is_still_stuck = (button_reply_id or "").strip().lower() == "not_resolved" or (
        is_unresolved_negative_only(current_message, button_reply_id)
    )
    if not is_still_stuck:
        return False

    # Episode FSM is authoritative when it already decided handoff
    if episode_handoff:
        return True

    # Diagnostic / refined retries are owned by generate_reply — do not hand off yet
    phase = (unresolved_phase or UNRESOLVED_PHASE_NONE).strip().lower()
    if phase in {UNRESOLVED_PHASE_DIAGNOSTIC, UNRESOLVED_PHASE_REFINED_RETRY}:
        return False

    if is_still_stuck and _last_bot_was_refined_unresolved_steps(last_bot_message_source):
        return True

    if issue_followup_depth < 3:
        return False
    if (button_reply_id or "").strip().lower() == "not_resolved":
        return True
    return is_unresolved_negative_only(current_message, button_reply_id)


def retrieve_for_refined_unresolved_turn(
    *,
    history: list[dict],
    current_message: str,
    pending_user_issue: str,
    platform: str,
    use_platform_only_retrieval: bool = False,
    known_os_version: str = "",
    known_model: str = "",
) -> dict:
    """
    Run deterministic RAG retrieval for refined Still Stuck retry.

    On success returns: {"context": str, "sources": list[str], "confidence_score": float|None}
    On failure returns a generate_reply-shaped dict with "kind".
    """
    from app.services.chat.reply import (
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
    os_version = (known_os_version or chat_ctx.get("known_os_version") or "").strip().lower()
    model = (known_model or chat_ctx.get("known_model") or "").strip().lower()
    if use_platform_only_retrieval:
        os_version = ""
        model = ""

    retrieval_os, retrieval_version = _map_retrieval_os_and_version(
        platform_slug, os_version, model
    )
    issue = (pending_user_issue or "").strip()
    # Never let a vague OS reply become the retrieval query
    if not issue or len(issue) < 12:
        issue = (pending_user_issue or current_message or "").strip()
    base_query = (
        f"PRIMARY PHONE ISSUE (solve ONLY this — do not switch topics): {issue}\n"
        "Return troubleshooting for this issue alone. "
        "Ignore unrelated problems from older chat turns."
    )
    prior_steps = _last_assistant_troubleshooting_from_history(history)
    if prior_steps and issue and _prior_steps_match_issue(prior_steps, issue):
        base_query = (
            f"{base_query}\n"
            "User already tried the steps below without success — "
            "need a DIFFERENT approach (alternate settings path or diagnostic):\n"
            f"{prior_steps[:700]}"
        )
    elif prior_steps and issue and not _prior_steps_match_issue(prior_steps, issue):
        logger.info(
            "refined_unresolved dropped mismatched prior_steps for issue=%r",
            issue[:80],
        )
    hint_lines = []
    if retrieval_os:
        hint_lines.append(f"os_family: {retrieval_os}")
    if retrieval_version:
        hint_lines.append(f"os_version: {retrieval_version}")
    if known_model and platform_slug == "samsung":
        hint_lines.append(f"device_model: {model}")
    refinement = (current_message or "").strip()
    if (
        refinement
        and refinement != issue
        and len(refinement) >= 8
        and "still stuck" not in refinement.lower()
    ):
        hint_lines.append(f"user_refinement: {refinement}")
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
