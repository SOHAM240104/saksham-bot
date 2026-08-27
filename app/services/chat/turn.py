"""Decide what Saksham should reply for one persisted inbound message."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import desc

from app.config.base import SessionLocal
from app.models.chat.chat import Conversation, Message, Thread
from app.models.senior import Senior
from app.services.chat.outbound import (
    buttons_outbound,
    handoff_outbound,
    outcome_from_llm_reply,
    platform_picker_outbound,
    text_outbound,
    turn_outcome,
    default_button_caption,
)
from app.services.chat.copy import dynamic_copy
from app.services.chat.episode import (
    AWAITING_FEEDBACK,
    AWAITING_MODE,
    BRANCH_NONE,
    BRANCH_SCAM,
    BRANCH_TECH,
    Episode,
    PHASE_IDLE,
    PHASE_TROUBLESHOOTING,
    episode_after_outbound,
    empty_episode,
    is_ack_only,
    is_strong_issue_fixed,
    may_resolve,
    parse_episode,
    reset_for_mode_chip,
    still_stuck_transition,
    tag_message_source,
)
from app.services.chat.platform_refinement import check_platform_refinement_from_extraction
from app.services.chat.reply import (
    _assistant_in_troubleshooting_arc,
    _build_history_messages,
    _is_on_tech_path,
    _is_platform_only_user_message,
    is_unresolved_negative_only,
    _name_for_thread,
    _pending_user_issue_from_history,
    _platform_label,
    _platform_slug_from_senior,
    _platform_slug_from_turn,
    _prior_thread_snapshot,
    _senior_for_thread,
    _unsupported_refinement_declined_reply,
    _unsupported_refinement_offer_reply,
    classify_turn_intent,
    conversation_control,
    extract_os_context_llm,
    generate_reply,
    refinement_platform_for_turn,
    resolve_issue_continuity,
    resolve_scam_context_from_turn,
    save_senior_platform_from_turn,
    thread_has_platform_issue_prompt,
)
from app.services.chat.still_stuck import (
    resolve_unresolved_phase,
    should_auto_handoff_by_unresolved_depth,
)
from app.services.chat.threads import (
    escalate_chatbot_thread_to_human,
    reassign_message_to_thread,
    resolve_chatbot_thread_and_start_new,
)
from app.services.chat.ux_signals import (
    is_bank_name_only,
)

logger = logging.getLogger("app.services.chat.turn")


def _log_timing(stage: str, t0: float, **extra: object) -> None:
    parts = " ".join(f"{k}={v}" for k, v in extra.items())
    logger.info(
        "TURN_TIMING stage=%s ms=%.0f%s",
        stage,
        (time.perf_counter() - t0) * 1000,
        f" {parts}" if parts else "",
    )


def _intent_from_selection(button_id: str, episode: Episode) -> dict:
    """
    Any selection_id owns the turn — no chip allowlist.

    Only map chips that already carry semantic intent for downstream gates.
    Branch for mode chips is set by reset_for_mode_chip, not here.
    """
    btn = (button_id or "").strip().lower()
    branch = (episode.branch or "").strip().lower()
    active = branch if branch in {BRANCH_TECH, BRANCH_SCAM} else "ambiguous"
    if btn == "request_human":
        return {"intent": "REQUEST_HUMAN", "active_branch": active}
    if btn == "resolved":
        return {"intent": "RESOLVED", "active_branch": active}
    return {"intent": "OTHER", "active_branch": active}


def is_handoff_confirmation_message(text: str) -> bool:
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


def is_human_request_message(text: str) -> bool:
    """True when the user is asking to talk to a person (not Still Stuck alone)."""
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not t:
        return False
    if any(
        p in t
        for p in (
            "anyone there",
            "is anyone there",
            "somebody help",
            "someone help",
            "need a human",
            "want a human",
            "talk to a human",
            "speak to a human",
            "talk to an agent",
            "speak to an agent",
            "connect me to someone",
            "connect me to a human",
            "connect me to an agent",
            "please connect me",
        )
    ):
        return True
    human = any(
        p in t
        for p in (
            "human",
            "agent",
            "tech saathi",
            "techsaathi",
            "real person",
            "customer care",
            "customer support",
            "talk to someone",
            "speak to someone",
            "connect me",
            "call me",
        )
    )
    ask = any(
        p in t
        for p in (
            "talk",
            "speak",
            "connect",
            "call",
            "want",
            "need",
            "please",
            "transfer",
        )
    )
    return human and ask


def is_greeting_only_message(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(".!? ")
    if not t or len(t) > 24:
        return False
    return t in {
        "hi",
        "hello",
        "hey",
        "hii",
        "hiii",
        "namaste",
        "namaskar",
        "good morning",
        "good afternoon",
        "good evening",
        "yo",
    }


def yes_no_confirm_decision(text: str) -> str:
    """Classify short Yes/No confirms. Uses whole tokens — never bare substring 'y'/'ha'."""
    msg = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    msg = re.sub(r"\s+", " ", msg).strip()
    if not msg:
        return "UNCLEAR"
    tokens = set(msg.split())
    yes_tokens = {"yes", "y", "haan", "ha", "han", "ok", "okay", "sure", "ji"}
    no_tokens = {"no", "n", "nah", "nope", "nahi", "na"}
    # Short whole-message yes (incl. soft connect after handoff ask)
    if msg in {
        "yes",
        "y",
        "haan",
        "ha",
        "han",
        "ok",
        "okay",
        "sure",
        "ji",
        "yes please",
        "haan ji",
        "yes ji",
        "connect",
        "transfer",
        "yes connect",
        "please connect",
        "connect me",
    }:
        return "YES"
    if msg in {
        "no",
        "n",
        "nah",
        "nope",
        "nahi",
        "na",
        "not now",
        "no thanks",
        "no thank you",
        "dont",
        "don't",
        "later",
    }:
        return "NO"
    # Multi-word but clearly affirmative / negative and still short
    if len(msg) <= 28:
        if tokens & yes_tokens and not (tokens & no_tokens):
            return "YES"
        if tokens & no_tokens and not (tokens & yes_tokens):
            return "NO"
    if any(p in msg for p in ("not now", "no thanks", "don't want", "dont want")):
        return "NO"
    return "UNCLEAR"


@dataclass
class TurnContext:
    message_id: int
    thread_id: int
    conversation_id: int
    user_message: str
    button_id: str = ""
    classifier_hints: dict = field(default_factory=dict)
    skip_intent_classifier: bool = False


def _classifier_payload(classifier_hints: dict) -> dict:
    return {
        "type": str(classifier_hints.get("payload_type") or ""),
        "eventType": str(classifier_hints.get("payload_event_type") or ""),
    }


def _classify_turn_intent_in_thread(
    thread_id: int,
    message_id: int,
    user_message: str,
    classifier_hints: dict,
    button_id: str | None,
    history_for_llm: list[dict] | None,
) -> dict:
    db = SessionLocal()
    try:
        result = asyncio.run(
            classify_turn_intent(
                db,
                thread_id=thread_id,
                current_message_id=message_id,
                current_message=user_message,
                payload=_classifier_payload(classifier_hints),
                button_reply_id=button_id,
                history=history_for_llm,
            )
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _extract_os_context_in_thread(
    user_message: str,
    history_for_llm: list[dict] | None,
    stored_platform: str,
    button_id: str | None,
) -> dict:
    return asyncio.run(
        extract_os_context_llm(
            user_message,
            history_for_llm,
            stored_platform=stored_platform or "",
            button_reply_id=button_id or "",
        )
    )


def _generate_reply_in_thread(
    thread_id: int,
    message_id: int,
    user_message: str,
    turn_meta: dict,
    history_for_llm: list[dict] | None,
) -> dict:
    db = SessionLocal()
    try:
        result = asyncio.run(
            generate_reply(
                db,
                thread_id,
                message_id,
                user_message,
                turn_meta=turn_meta,
                history=history_for_llm,
            )
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _stamp_outbound(outbound: list | None, episode: Episode) -> list:
    stamped: list = []
    for item in outbound or []:
        if not isinstance(item, dict):
            stamped.append(item)
            continue
        copy = dict(item)
        src = copy.get("message_source")
        if src is not None or "message_source" in copy:
            copy["message_source"] = tag_message_source(src or "", episode)
        stamped.append(copy)
    return stamped


def _outcome(
    *,
    outbound=None,
    thread_id: int | None = None,
    bot_response: str | None = None,
    message_source: str | None = None,
    confidence_score: float | None = None,
    patch_message_source_only: bool = False,
    episode: Episode | None = None,
    action: str = "",
    kind: str = "",
) -> dict:
    ep = episode or empty_episode()
    # Stamp the episode that this outbound leaves open for the next turn
    next_ep = episode_after_outbound(
        prior=ep,
        message_source=message_source or "",
        action=action,
        kind=kind,
        forced_branch=ep.branch if ep.branch in {BRANCH_TECH, BRANCH_SCAM} else None,
    )
    tagged_source = (
        tag_message_source(message_source, next_ep)
        if message_source is not None
        else None
    )
    return turn_outcome(
        outbound=_stamp_outbound(outbound, next_ep),
        thread_id=thread_id,
        bot_response=bot_response,
        message_source=tagged_source,
        confidence_score=confidence_score,
        patch_message_source_only=patch_message_source_only,
    )


async def process_inbound_turn(turn: TurnContext) -> dict:
    """
    Decide Saksham's reply for one inbound message already saved in the DB.

    Covers routing (tech/scam), platform/refinement gates, Still Stuck ladder,
    handoff decisions, and LLM/RAG generation. Returns what to send — no I/O.
    """
    turn_t0 = time.perf_counter()
    message_id = turn.message_id
    thread_id = turn.thread_id
    conversation_id = turn.conversation_id
    user_message = turn.user_message
    button_id = turn.button_id or ""

    history_for_llm: list[dict] | None = None
    db = SessionLocal()
    try:
        t_hist = time.perf_counter()
        history_for_llm = _build_history_messages(
            db, thread_id, message_id, user_message
        )
        db.commit()
        _log_timing("history_load", t_hist, message_id=message_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    platform_saved_slug = ""
    db = SessionLocal()
    try:
        senior = _senior_for_thread(db, thread_id)
        if senior and _is_platform_only_user_message(user_message, button_id):
            picked_slug = save_senior_platform_from_turn(
                db, senior, user_message, button_id
            )
            if not picked_slug:
                picked_slug = _platform_slug_from_turn(user_message, button_id)
            platform_saved_slug = picked_slug
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    intent_result: dict = {}
    classified_intent = ""
    issue_followup_depth = 1
    same_issue_as_previous = False
    is_unresolved_followup = False
    awaiting_handoff_confirm = False
    awaiting_platform_confirm = False
    awaiting_unsupported_refinement_confirm = False
    confirm_reply = ""
    platform_confirm_reply = ""
    unsupported_refinement_reply = ""
    use_platform_only_retrieval = False
    last_bot_response = ""
    last_bot_message_source = ""
    declined_handoff = False
    ask_handoff_confirmation = False
    override = None
    os_extraction: dict = {}
    support_mode = ""
    scam_context = False

    # Episode-first: peek prior bot turn before any LLM so selection routing is cheap.
    t_ep = time.perf_counter()
    db = SessionLocal()
    try:
        last_msg = (
            db.query(Message)
            .filter(
                Message.thread_id == thread_id,
                Message.is_deleted.is_(False),
                Message.id != message_id,
            )
            .order_by(desc(Message.created))
            .first()
        )
        last_bot_response = (last_msg.bot_response or "").strip() if last_msg else ""
        last_bot_message_source = (last_msg.message_source or "").strip() if last_msg else ""
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    _log_timing("episode_peek", t_ep, message_id=message_id)

    mode_reset_early = reset_for_mode_chip(button_id)
    if mode_reset_early is not None:
        episode_peek = mode_reset_early
    else:
        episode_peek = parse_episode(last_bot_message_source)

    # Chip selections own intent (no classifier). Free text keeps the classifier —
    # skipping it broke REQUEST_HUMAN, bye/resolve gates, and mid-flow hi hold.
    if button_id.strip() or turn.skip_intent_classifier:
        intent_result = _intent_from_selection(button_id, episode_peek)
        classified_intent = (intent_result.get("intent") or "OTHER").strip().upper()
        logger.info(
            "SELECTION_INTENT intent=%s active_branch=%s button=%s message_id=%s",
            classified_intent,
            intent_result.get("active_branch"),
            button_id[:32],
            message_id,
        )
    elif user_message.strip():
        t_clf = time.perf_counter()
        intent_result = await asyncio.to_thread(
            _classify_turn_intent_in_thread,
            thread_id,
            message_id,
            user_message,
            turn.classifier_hints,
            button_id,
            history_for_llm,
        )
        classified_intent = (intent_result.get("intent") or "").strip().upper()
        _log_timing(
            "classify_turn_intent",
            t_clf,
            message_id=message_id,
            intent=classified_intent,
        )
        logger.info(
            "LLM_INTENT intent=%s active_branch=%s message_id=%s",
            classified_intent,
            intent_result.get("active_branch"),
            message_id,
        )
    else:
        intent_result = {"intent": "OTHER", "active_branch": "ambiguous"}
        classified_intent = "OTHER"

    support_mode, scam_context = resolve_scam_context_from_turn(
        history_for_llm or [],
        user_message,
        button_id,
        intent_result=intent_result,
    )

    # Explicit Tech/Scam chip = new episode (code-owned; history must not override)
    mode_reset = mode_reset_early
    force_fresh_mode_entry = mode_reset is not None
    if mode_reset is not None:
        episode = mode_reset
        if episode.branch == BRANCH_TECH:
            support_mode = "tech"
            scam_context = False
            intent_result = {
                **intent_result,
                "active_branch": "tech",
                "intent": "OTHER",
            }
            classified_intent = "OTHER"
        elif episode.branch == BRANCH_SCAM:
            support_mode = "scam"
            scam_context = True
            intent_result = {
                **intent_result,
                "active_branch": "scam",
                "intent": "OTHER",
            }
            classified_intent = "OTHER"
    else:
        episode = episode_peek

    if (
        button_id in {"ios", "android"}
        and scam_context
        and (support_mode or "").strip().lower() != "tech"
    ):
        db = SessionLocal()
        try:
            customer_name = _name_for_thread(db, thread_id)
        finally:
            db.close()
        # After OS pick, ask for the story briefly — do not repeat full scam_entry opener
        entry_text = dynamic_copy(
            "scam_story_ask",
            context={"customer_name": (customer_name or "").strip()},
        )
        source = f"scam_os:{button_id},scam_story_ask,dynamic_copy"
        scam_ep = Episode(
            branch=BRANCH_SCAM,
            phase="scam_flow",
            awaiting="none",
            stuck_cycle=0,
        )
        return _outcome(
            outbound=[
                text_outbound(entry_text, message_source="scam_story_ask,dynamic_copy")
            ],
            bot_response=entry_text,
            message_source=source,
            thread_id=thread_id,
            episode=scam_ep,
        )

    # Bank clarify chip ids (mid-tech bank-name soft fork)
    if button_id == "continue_tech":
        # Stay on tech after bank-name clarify
        hold = (
            "OK — let's stay on your phone issue. "
            "Tap Resolved if it worked, or Still Stuck if you need more help."
        )
        return _outcome(
            outbound=[
                buttons_outbound(
                    hold,
                    "feedback_buttons",
                    message_source="feedback_buttons,branch_clarify_continue",
                )
            ],
            bot_response=hold,
            message_source="feedback_buttons,branch_clarify_continue",
            thread_id=thread_id,
            episode=Episode(
                branch=BRANCH_TECH,
                phase="troubleshooting",
                awaiting=AWAITING_FEEDBACK,
                stuck_cycle=0,
            ),
            action="feedback_buttons",
        )
    if button_id == "request_human":
        classified_intent = "REQUEST_HUMAN"
        intent_result = {**intent_result, "intent": "REQUEST_HUMAN"}

    logger.info(
        "ROUTE_CONTEXT message_id=%s button=%s support_mode=%s active_branch=%s scam_context=%s",
        message_id,
        button_id[:16],
        support_mode,
        (intent_result.get("active_branch") or ""),
        scam_context,
    )

    if scam_context or force_fresh_mode_entry:
        same_issue_as_previous = False
        issue_followup_depth = 1
        is_unresolved_followup = False
    elif _is_on_tech_path(
        support_mode,
        user_message,
        button_id,
        active_branch=(intent_result.get("active_branch") or "").strip().lower(),
    ):
        continuity = resolve_issue_continuity(
            history=history_for_llm or [],
            current_message=user_message,
            button_reply_id=button_id,
            on_tech_path=True,
        )
        same_issue_as_previous = bool(continuity.get("same_issue_as_previous"))
        issue_followup_depth = int(continuity.get("issue_followup_depth") or 1)
        is_unresolved_followup = bool(continuity.get("is_unresolved_followup"))
        if issue_followup_depth < 1:
            issue_followup_depth = 1
        logger.info(
            "ISSUE_CONTINUITY message_id=%s same_issue=%s depth=%s unresolved=%s",
            message_id,
            same_issue_as_previous,
            issue_followup_depth,
            is_unresolved_followup,
        )
    else:
        same_issue_as_previous = False
        issue_followup_depth = 1
        is_unresolved_followup = False

    # New phone topic mid-thread — reset Still Stuck ladder (do not inherit prior stuck).
    if (
        not force_fresh_mode_entry
        and not scam_context
        and not same_issue_as_previous
        and not is_unresolved_followup
        and button_id not in {"not_resolved", "resolved", "tech", "scam"}
        and episode.stuck_cycle > 0
    ):
        logger.info(
            "EPISODE_TOPIC_RESET message_id=%s prior_stuck=%s",
            message_id,
            episode.stuck_cycle,
        )
        episode = Episode(
            branch=BRANCH_TECH if episode.branch in {BRANCH_NONE, "none"} else episode.branch,
            phase=PHASE_TROUBLESHOOTING,
            awaiting="none",
            stuck_cycle=0,
        )

    # Derive awaiting flags from the episode peek (already loaded — no second DB hit).
    # Fresh Tech/Scam chip starts a new episode — ignore prior handoff/platform confirms.
    awaiting_handoff_confirm = False
    awaiting_platform_confirm = False
    awaiting_unsupported_refinement_confirm = False
    if not force_fresh_mode_entry:
        awaiting_handoff_confirm = bool(
            "handoff_confirmation" in last_bot_message_source
            or is_handoff_confirmation_message(last_bot_response)
            or episode.awaiting == "handoff_confirm"
        )
        awaiting_platform_confirm = bool(
            "platform_confirmation" in last_bot_message_source
            or episode.awaiting == "platform_confirm"
        )
        awaiting_unsupported_refinement_confirm = bool(
            "unsupported_refinement_confirmation" in last_bot_message_source
        )

    # Platform Yes/No owns the turn — never treat it as handoff confirm.
    if awaiting_platform_confirm:
        awaiting_handoff_confirm = False

    logger.info(
        "EPISODE message_id=%s branch=%s phase=%s awaiting=%s stuck=%s fresh_mode=%s",
        message_id,
        episode.branch,
        episode.phase,
        episode.awaiting,
        episode.stuck_cycle,
        force_fresh_mode_entry,
    )

    if awaiting_handoff_confirm:
        confirm_reply = yes_no_confirm_decision(user_message)
        declined_handoff = confirm_reply == "NO"
    if awaiting_platform_confirm:
        platform_confirm_reply = yes_no_confirm_decision(user_message)
    if awaiting_unsupported_refinement_confirm:
        unsupported_refinement_reply = yes_no_confirm_decision(user_message)
        if unsupported_refinement_reply == "YES":
            use_platform_only_retrieval = True
            issue_followup_depth = max(issue_followup_depth, 3)
        elif unsupported_refinement_reply == "NO":
            pass

    # Free-text human ask beats Still Stuck phrasing ("still stuck please connect me…").
    if (
        not force_fresh_mode_entry
        and button_id not in {"resolved", "not_resolved", "tech", "scam"}
        and not awaiting_platform_confirm
        and not awaiting_unsupported_refinement_confirm
        and is_human_request_message(user_message)
    ):
        classified_intent = "REQUEST_HUMAN"
        intent_result = {**intent_result, "intent": "REQUEST_HUMAN"}

    is_unresolved = button_id == "not_resolved" or is_unresolved_followup
    if scam_context:
        is_unresolved = False
        issue_followup_depth = 1

    on_tech_path_for_phase = _is_on_tech_path(
        support_mode,
        user_message,
        button_id,
        active_branch=(intent_result.get("active_branch") or "").strip().lower(),
    )

    episode_stuck_action = ""
    if (
        on_tech_path_for_phase
        and not scam_context
        and classified_intent != "REQUEST_HUMAN"
        and (
            button_id == "not_resolved"
            or is_unresolved_negative_only(user_message, button_id)
        )
    ):
        episode, episode_stuck_action = still_stuck_transition(episode)
        same_issue_as_previous = True
        is_unresolved = True
        if episode_stuck_action == "handoff":
            issue_followup_depth = max(issue_followup_depth, 3)
        elif episode_stuck_action == "refined":
            issue_followup_depth = max(issue_followup_depth, 2)
        logger.info(
            "EPISODE_STILL_STUCK action=%s stuck=%s phase=%s",
            episode_stuck_action,
            episode.stuck_cycle,
            episode.phase,
        )

    unresolved_phase = resolve_unresolved_phase(
        on_tech_path=on_tech_path_for_phase,
        scam_mode=bool(scam_context),
        same_issue_as_previous=same_issue_as_previous or bool(episode_stuck_action),
        button_reply_id=button_id,
        current_message=user_message,
        last_bot_message_source=last_bot_message_source,
        last_bot_response=last_bot_response,
        episode_action=episode_stuck_action,
    )
    logger.info(
        "UNRESOLVED_PHASE phase=%s message_id=%s depth=%s",
        unresolved_phase,
        message_id,
        issue_followup_depth,
    )

    allow_resolve = may_resolve(
        episode=episode,
        button_id=button_id,
        user_message=user_message,
        suggest_intent=classified_intent,
        scam_context=bool(scam_context),
    )
    if classified_intent == "RESOLVED" and allow_resolve:
        control_result = conversation_control.invoke({"action": "resolved"})
        override = {
            "kind": "control",
            "action": (control_result.get("action") or "").strip(),
            "message_source": "intent_classifier",
            "confidence_score": None,
        }
    elif classified_intent == "RESOLVED" and not allow_resolve:
        logger.info(
            "RESOLVE_BLOCKED message_id=%s awaiting=%s phase=%s msg=%r",
            message_id,
            episode.awaiting,
            episode.phase,
            (user_message or "")[:40],
        )
        classified_intent = "OTHER"

    # Soft mid-flow ack (ok/okay) or greeting — gentle hold; avoid RAG issue bleed
    if (
        not override
        and episode.awaiting == AWAITING_FEEDBACK
        and not scam_context
        and button_id not in {"resolved", "not_resolved"}
        and classified_intent != "RESOLVED"
        and classified_intent != "REQUEST_HUMAN"
        and (
            is_ack_only(user_message)
            or is_greeting_only_message(user_message)
        )
        and not is_unresolved_negative_only(user_message, button_id)
    ):
        db = SessionLocal()
        try:
            customer_name = _name_for_thread(db, thread_id)
        finally:
            db.close()
        feedback_text = dynamic_copy(
            "feedback_ack_hold",
            context={"customer_name": (customer_name or "").strip()},
        )
        return _outcome(
            outbound=[
                buttons_outbound(
                    feedback_text,
                    "feedback_buttons",
                    message_source="feedback_buttons,ack_hold,dynamic_copy",
                )
            ],
            bot_response=feedback_text,
            message_source="feedback_buttons,ack_hold,dynamic_copy",
            thread_id=thread_id,
            episode=episode,
            action="feedback_buttons",
        )

    # User declined handoff confirm — clear awaiting and stay helpful (do not keep handoff_confirm)
    if (
        not override
        and awaiting_handoff_confirm
        and confirm_reply == "NO"
        and not scam_context
    ):
        db = SessionLocal()
        try:
            customer_name = _name_for_thread(db, thread_id)
        finally:
            db.close()
        hold = dynamic_copy(
            "feedback_ack_hold",
            context={"customer_name": (customer_name or "").strip()},
        )
        if not (hold or "").strip():
            hold = (
                "No problem — I'm still here. "
                "Tell me what you'd like to try next, or tap Still Stuck if you need more help."
            )
        return _outcome(
            outbound=[
                buttons_outbound(
                    hold,
                    "feedback_buttons",
                    message_source="feedback_buttons,handoff_declined,dynamic_copy",
                )
            ],
            bot_response=hold,
            message_source="feedback_buttons,handoff_declined,dynamic_copy",
            thread_id=thread_id,
            episode=Episode(
                branch=BRANCH_TECH if episode.branch == BRANCH_NONE else episode.branch,
                phase=PHASE_TROUBLESHOOTING
                if episode.phase in {PHASE_IDLE, "idle", ""}
                else episode.phase,
                awaiting=AWAITING_FEEDBACK,
                stuck_cycle=episode.stuck_cycle,
            ),
            action="feedback_buttons",
        )

    # Idle / mode greeting — welcome + Tech/Scam chips (do not ask for "more detail")
    if (
        not override
        and not scam_context
        and not button_id
        and not awaiting_handoff_confirm
        and not awaiting_platform_confirm
        and not awaiting_unsupported_refinement_confirm
        and classified_intent != "REQUEST_HUMAN"
        and is_greeting_only_message(user_message)
        and episode.awaiting != AWAITING_FEEDBACK
        and episode.phase not in {
            "troubleshooting",
            "diagnostic",
            "refined",
            "intake",
            "scam_flow",
        }
        and (
            episode.awaiting == AWAITING_MODE
            or (
                episode.phase == PHASE_IDLE
                and episode.branch in {BRANCH_NONE, "none"}
            )
        )
    ):
        db = SessionLocal()
        try:
            snap = _prior_thread_snapshot(db, thread_id)
            welcome = (snap.get("prior_welcome_blend") or "").strip()
        finally:
            db.close()
        if not welcome:
            welcome = dynamic_copy("welcome_back_context")
        if not (welcome or "").strip():
            welcome = default_button_caption("mode_buttons", feedback_checkin="")
        return _outcome(
            outbound=[
                buttons_outbound(
                    welcome,
                    "mode_buttons",
                    message_source="mode_buttons,greeting,dynamic_copy",
                )
            ],
            bot_response=welcome,
            message_source="mode_buttons,greeting,dynamic_copy",
            thread_id=thread_id,
            episode=Episode(
                branch=BRANCH_NONE,
                phase=PHASE_IDLE,
                awaiting=AWAITING_MODE,
                stuck_cycle=0,
            ),
            action="mode_buttons",
        )

    # Bank name mid-tech → soft clarify chips (do not jump into scam)
    if (
        not override
        and not scam_context
        and episode.branch in {BRANCH_TECH, "none", BRANCH_NONE}
        and (
            episode.awaiting == AWAITING_FEEDBACK
            or episode.phase in {"troubleshooting", "diagnostic", "refined"}
        )
        and is_bank_name_only(user_message)
        and button_id not in {"resolved", "not_resolved", "tech", "scam"}
    ):
        clarify = dynamic_copy("branch_clarify_bank")
        return _outcome(
            outbound=[
                buttons_outbound(
                    clarify,
                    "branch_clarify_buttons",
                    message_source="branch_clarify_buttons,bank_mid_tech",
                )
            ],
            bot_response=clarify,
            message_source="branch_clarify_buttons,bank_mid_tech",
            thread_id=thread_id,
            episode=episode,
            action="branch_clarify_buttons",
        )

    handoff_needed = False
    handoff_count = 0
    switched_from_human_to_chatbot_this_turn = False

    # Episode FSM already chose handoff — do not fall through to RAG/OS extract.
    if episode_stuck_action == "handoff" and not scam_context:
        db = SessionLocal()
        try:
            hid = escalate_chatbot_thread_to_human(db, thread_id)
            if hid:
                reassign_message_to_thread(db, message_id, hid)
                thread_id = hid
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        wait_text = dynamic_copy("handoff_wait")
        logger.info(
            "handoff_triggered thread_id=%s signal_count=%s reason=episode_fsm",
            thread_id,
            max(issue_followup_depth, 1),
        )
        return _outcome(
            outbound=[
                handoff_outbound(wait_text, handoff_count=max(issue_followup_depth, 1))
            ],
            bot_response=wait_text,
            message_source="handoff",
            thread_id=thread_id,
            episode=episode,
        )

    db = SessionLocal()
    try:
        thread_row = db.query(Thread).filter(Thread.id == thread_id).first()

        if thread_row and thread_row.role == "chatbot" and thread_row.status == "assigned":
            classifier_requested_handoff = (
                classified_intent == "REQUEST_HUMAN"
                and not declined_handoff
                # Platform Yes/No owns the turn — do not steal into handoff confirm
                and not awaiting_platform_confirm
                and not awaiting_unsupported_refinement_confirm
                and not force_fresh_mode_entry
            )
            if (
                awaiting_handoff_confirm
                and confirm_reply == "YES"
                and not switched_from_human_to_chatbot_this_turn
            ):
                hid = escalate_chatbot_thread_to_human(
                    db, thread_id
                )
                if hid:
                    reassign_message_to_thread(db, message_id, hid)
                    thread_id = hid
                handoff_needed = True
                handoff_count = 1
            elif (
                classifier_requested_handoff
                and not switched_from_human_to_chatbot_this_turn
            ):
                ask_handoff_confirmation = True
            elif should_auto_handoff_by_unresolved_depth(
                scam_mode=bool(scam_context),
                on_tech_path=on_tech_path_for_phase,
                same_issue_as_previous=same_issue_as_previous or bool(episode_stuck_action),
                button_reply_id=button_id,
                current_message=user_message,
                issue_followup_depth=issue_followup_depth,
                unresolved_phase=unresolved_phase,
                last_bot_message_source=last_bot_message_source,
                episode_handoff=(episode_stuck_action == "handoff"),
            ):
                hid = escalate_chatbot_thread_to_human(
                    db, thread_id
                )
                if hid:
                    reassign_message_to_thread(db, message_id, hid)
                    thread_id = hid
                handoff_needed = True
                handoff_count = issue_followup_depth
                logger.info(
                    "handoff_by_issue_followup_depth thread_id=%s depth=%s",
                    thread_id,
                    issue_followup_depth,
                )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # Lazy: only call dynamic_copy when a branch needs this string (keeps warmth, cuts waste).
    feedback_checkin_holder: dict[str, str] = {}

    def _feedback_checkin() -> str:
        if "text" not in feedback_checkin_holder:
            feedback_checkin_holder["text"] = dynamic_copy("feedback_checkin")
        return feedback_checkin_holder["text"]

    # Platform confirm Yes/No must run before handoff ask — "yes" means phone, not human.
    platform_confirm_unclear = False
    if awaiting_platform_confirm:
        db = SessionLocal()
        try:
            senior = _senior_for_thread(db, thread_id)
            stored_slug = _platform_slug_from_senior(db, senior)
            customer_name = _name_for_thread(db, thread_id)
        finally:
            db.close()
        copy_ctx = {
            "platform_label": _platform_label(stored_slug),
            "customer_name": customer_name,
        }
        if platform_confirm_reply == "UNCLEAR":
            # Do not re-ask in code — LLM judges greeting / soft yes-no / new topic
            platform_confirm_unclear = True
            awaiting_platform_confirm = False
        elif platform_confirm_reply == "NO":
            list_msg = default_button_caption("platform_buttons", feedback_checkin="")
            return _outcome(
                outbound=[platform_picker_outbound(list_msg, message_source="platform_list")],
                bot_response=list_msg,
                message_source="platform_list",
                thread_id=thread_id,
                episode=episode,
            )
        elif platform_confirm_reply == "YES":
            issue_text = dynamic_copy("platform_issue_ask", context=copy_ctx)
            return _outcome(
                outbound=[text_outbound(issue_text, message_source="platform_issue_prompt,dynamic_copy")],
                bot_response=issue_text,
                message_source="platform_issue_prompt,dynamic_copy",
                thread_id=thread_id,
                episode=episode,
            )

    if awaiting_handoff_confirm and confirm_reply == "UNCLEAR":
        confirm_text = dynamic_copy("handoff_confirm")
        _log_timing("turn_total", turn_t0, message_id=message_id, path="handoff_reask")
        return _outcome(
            outbound=[text_outbound(confirm_text, message_source="handoff_confirmation")],
            bot_response=confirm_text, message_source="handoff_confirmation",
            thread_id=thread_id,
            episode=episode,
        )

    if awaiting_unsupported_refinement_confirm:
        db = SessionLocal()
        try:
            senior = _senior_for_thread(db, thread_id)
            stored_slug = _platform_slug_from_senior(db, senior)
            customer_name = _name_for_thread(db, thread_id)
        finally:
            db.close()
        if unsupported_refinement_reply == "UNCLEAR":
            confirm_text = last_bot_response or (
                "Would you like me to try general steps without that version? "
                "Please reply Yes or No."
            )
            return _outcome(
                outbound=[text_outbound(confirm_text, message_source="unsupported_refinement_confirmation,dynamic_copy")],
                bot_response=confirm_text,
                message_source="unsupported_refinement_confirmation,dynamic_copy",
                thread_id=thread_id,
                episode=episode,
            )
        if unsupported_refinement_reply == "NO":
            declined = _unsupported_refinement_declined_reply(
                customer_name=customer_name,
                platform=stored_slug,
            )
            decline_text = (declined.get("message") or "").strip()
            return _outcome(
                outbound=[text_outbound(decline_text, message_source="unsupported_refinement_declined,dynamic_copy")],
                bot_response=decline_text,
                message_source="unsupported_refinement_declined,dynamic_copy",
                thread_id=thread_id,
                episode=episode,
            )

    if handoff_needed:
        wait_text = dynamic_copy("handoff_wait")
        logger.info(
            "handoff_triggered thread_id=%s signal_count=%s",
            thread_id,
            handoff_count,
        )
        return _outcome(
            outbound=[handoff_outbound(wait_text, handoff_count=handoff_count)],
            bot_response=wait_text,
            message_source="handoff",
            thread_id=thread_id,
            episode=episode,
        )

    if ask_handoff_confirmation or (
        classified_intent == "REQUEST_HUMAN"
        and not awaiting_handoff_confirm
        and not awaiting_platform_confirm
        and not awaiting_unsupported_refinement_confirm
        and not declined_handoff
        and not switched_from_human_to_chatbot_this_turn
    ):
        confirm_text = dynamic_copy("handoff_confirm")
        return _outcome(
            outbound=[text_outbound(confirm_text, message_source="handoff_confirmation")],
            bot_response=confirm_text, message_source="handoff_confirmation",
            thread_id=thread_id,
            episode=episode,
        )

    if platform_saved_slug:
        on_tech_path_now = _is_on_tech_path(
            support_mode,
            user_message,
            button_id,
            active_branch=(intent_result.get("active_branch") or "").strip().lower(),
        )
        pending_issue = _pending_user_issue_from_history(
            history_for_llm or [],
            user_message,
            button_id,
        )
        # Mode chip starts a fresh intake — do not inherit prior issue for this gate
        if force_fresh_mode_entry:
            pending_issue = ""
        if on_tech_path_now and not scam_context and not pending_issue:
            db = SessionLocal()
            try:
                customer_name = _name_for_thread(db, thread_id)
            finally:
                db.close()
            issue_text = dynamic_copy(
                "platform_issue_ask",
                context={
                    "platform_label": _platform_label(platform_saved_slug),
                    "customer_name": customer_name,
                },
            )
            return _outcome(
                outbound=[text_outbound(issue_text, message_source="platform_issue_prompt,dynamic_copy")],
                bot_response=issue_text,
                message_source="platform_issue_prompt,dynamic_copy",
                thread_id=thread_id,
                episode=episode,
            )

    if (
        not scam_context
        and not awaiting_handoff_confirm
        and not awaiting_platform_confirm
        and not awaiting_unsupported_refinement_confirm
        and not use_platform_only_retrieval
    ):
        on_tech_path_now = _is_on_tech_path(
            support_mode,
            user_message,
            button_id,
            active_branch=(intent_result.get("active_branch") or "").strip().lower(),
        )
        db = SessionLocal()
        try:
            refinement_platform = refinement_platform_for_turn(
                db, thread_id, history_for_llm or []
            )
            customer_name = _name_for_thread(db, thread_id)
            senior_row = _senior_for_thread(db, thread_id)
            stored_platform = _platform_slug_from_senior(db, senior_row) or ""
        finally:
            db.close()
        pending_for_os = _pending_user_issue_from_history(
            history_for_llm or [],
            user_message,
            button_id,
        )
        if (
            on_tech_path_now
            and refinement_platform
            and refinement_platform != "samsung"
            and (pending_for_os or is_unresolved)
        ):
            os_extraction = await asyncio.to_thread(
                _extract_os_context_in_thread,
                user_message,
                history_for_llm,
                stored_platform,
                button_id,
            )
        if on_tech_path_now and refinement_platform:
            refinement_check = check_platform_refinement_from_extraction(
                os_extraction,
                refinement_platform,
                text=user_message,
            )
            if refinement_check.get("status") == "unsupported":
                offer = _unsupported_refinement_offer_reply(
                    customer_name=customer_name,
                    platform=refinement_platform,
                    unsupported_label=refinement_check.get("label") or "",
                    refinement_type=refinement_check.get("refinement_type") or "",
                )
                offer_text = (offer.get("message") or "").strip()
                return _outcome(
                    outbound=[
                        text_outbound(
                            offer_text,
                            message_source="unsupported_refinement_confirmation,dynamic_copy",
                        )
                    ],
                    bot_response=offer_text,
                    message_source="unsupported_refinement_confirmation,dynamic_copy",
                    thread_id=thread_id,
                    episode=episode,
                )

    if (
        not scam_context
        and not awaiting_handoff_confirm
        and not awaiting_platform_confirm
        and not awaiting_unsupported_refinement_confirm
    ):
        on_tech_path_now = _is_on_tech_path(
            support_mode,
            user_message,
            button_id,
            active_branch=(intent_result.get("active_branch") or "").strip().lower(),
        )
        pending_issue = _pending_user_issue_from_history(
            history_for_llm or [],
            user_message,
            button_id,
        )
        if force_fresh_mode_entry:
            pending_issue = ""
        platform_pick_this_turn = _is_platform_only_user_message(
            user_message, button_id
        )
        db = SessionLocal()
        try:
            senior = _senior_for_thread(db, thread_id)
            stored_slug = _platform_slug_from_senior(db, senior)
            already_past_confirm = thread_has_platform_issue_prompt(
                db, thread_id, exclude_message_id=message_id
            )
        finally:
            db.close()
        if force_fresh_mode_entry:
            already_past_confirm = False
        # Structural only: phone confirm after Tech Help chip — not from history/classifier.
        if (
            (
                force_fresh_mode_entry
                or (button_id or "").strip().lower() == "tech"
            )
            and on_tech_path_now
            and senior
            and senior.device_id
            and stored_slug
            and not pending_issue
            and not platform_pick_this_turn
            and not already_past_confirm
        ):
            db = SessionLocal()
            try:
                customer_name = _name_for_thread(db, thread_id)
            finally:
                db.close()
            confirm_text = dynamic_copy(
                "platform_still_using",
                context={
                    "platform_label": _platform_label(stored_slug),
                    "customer_name": customer_name,
                },
            )
            return _outcome(
                outbound=[text_outbound(confirm_text, message_source="platform_confirmation")],
                bot_response=confirm_text,
                message_source="platform_confirmation",
                thread_id=thread_id,
                episode=episode,
            )

    fresh_chatbot_thread = False
    post_resolve_welcome_sent = False
    db = SessionLocal()
    try:
        thread_row = db.query(Thread).filter(Thread.id == thread_id).first()
        prior_msg_in_same_thread = (
            db.query(Message)
            .filter(
                Message.thread_id == thread_id,
                Message.is_deleted.is_(False),
                Message.id != message_id,
            )
            .first()
        )
        prior_thread_exists = (
            db.query(Thread)
            .filter(
                Thread.conversation_id == conversation_id,
                Thread.id != thread_id,
            )
            .first()
        )
        fresh_chatbot_thread = bool(
            thread_row
            and thread_row.role == "chatbot"
            and not prior_msg_in_same_thread
            and prior_thread_exists
        )
        if is_unresolved or _assistant_in_troubleshooting_arc(
            last_bot_message_source, last_bot_response
        ):
            fresh_chatbot_thread = False
        if fresh_chatbot_thread:
            prev_thread = (
                db.query(Thread)
                .filter(
                    Thread.conversation_id == conversation_id,
                    Thread.id != thread_id,
                )
                .order_by(desc(Thread.created))
                .first()
            )
            if prev_thread:
                last_on_prev = (
                    db.query(Message)
                    .filter(
                        Message.thread_id == prev_thread.id,
                        Message.is_deleted.is_(False),
                    )
                    .order_by(desc(Message.created))
                    .first()
                )
                if last_on_prev and "post_resolve_welcome" in (
                    last_on_prev.message_source or ""
                ):
                    post_resolve_welcome_sent = True
        if not post_resolve_welcome_sent:
            prior_bot_welcome = (
                db.query(Message)
                .filter(
                    Message.thread_id == thread_id,
                    Message.is_deleted.is_(False),
                    Message.id != message_id,
                    Message.bot_response.isnot(None),
                )
                .order_by(desc(Message.created))
                .first()
            )
            if prior_bot_welcome and "post_resolve_welcome" in (
                prior_bot_welcome.message_source or ""
            ):
                post_resolve_welcome_sent = True
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    turn_meta = {
        "button_reply_id": button_id,
        "unresolved_signal_current_turn": is_unresolved,
        "unresolved_rounds": issue_followup_depth,
        "unresolved_phase": unresolved_phase,
        "use_platform_only_retrieval": use_platform_only_retrieval,
        "fresh_chatbot_thread": fresh_chatbot_thread,
        "post_resolve_welcome_sent": post_resolve_welcome_sent,
        "active_tech_troubleshooting_arc": (
            episode.awaiting == AWAITING_FEEDBACK
            or episode.phase in {"troubleshooting", "diagnostic", "refined"}
            or _assistant_in_troubleshooting_arc(
                last_bot_message_source, last_bot_response
            )
        ),
        "active_branch": (intent_result.get("active_branch") or "").strip().lower(),
        "classifier_intent": (intent_result.get("intent") or "").strip().upper(),
        "os_extraction": os_extraction,
        "force_fresh_mode_entry": force_fresh_mode_entry,
        "explicit_tech_entry": force_fresh_mode_entry
        or (button_id or "").strip().lower() == "tech",
        "platform_confirm_unclear": platform_confirm_unclear,
        "branch_locked": force_fresh_mode_entry
        or (
            episode.branch in {BRANCH_TECH, BRANCH_SCAM}
            and not platform_confirm_unclear
        ),
        "episode_branch": episode.branch,
        "episode_phase": episode.phase,
        "episode_awaiting": episode.awaiting,
    }
    t_gen = time.perf_counter()
    llm_result = override or await asyncio.to_thread(
        _generate_reply_in_thread,
        thread_id,
        message_id,
        user_message,
        turn_meta,
        history_for_llm,
    )
    _log_timing(
        "generate_reply",
        t_gen,
        message_id=message_id,
        kind=(llm_result.get("kind") if isinstance(llm_result, dict) else ""),
    )

    new_thread_id = None
    welcome_text = default_button_caption("mode_buttons", feedback_checkin="")
    resolved_ack = ""

    if llm_result.get("kind") == "control" and (
        (llm_result.get("action") or "").strip().lower() == "resolved"
    ):
        # LLM resolve is advisory — same hard gate as classifier
        if not may_resolve(
            episode=episode,
            button_id=button_id,
            user_message=user_message,
            suggest_intent="RESOLVED",
            scam_context=bool(scam_context),
        ):
            logger.info(
                "LLM_RESOLVE_BLOCKED message_id=%s awaiting=%s phase=%s",
                message_id,
                episode.awaiting,
                episode.phase,
            )
            llm_result = {
                "kind": "text",
                "message": (
                    "Whenever you're ready — tap Resolved if that fixed it, "
                    "or Still Stuck if you need more help."
                    if episode.awaiting == AWAITING_FEEDBACK
                    else "Could you tell me a bit more about what you need help with?"
                ),
                "message_source": "resolve_gate_blocked",
            }
            if episode.awaiting == AWAITING_FEEDBACK:
                llm_result = {
                    "kind": "action",
                    "action": "feedback_buttons",
                    "message": (
                        "Whenever you're ready — tap Resolved if that fixed it, "
                        "or Still Stuck if you need more help."
                    ),
                    "message_source": "feedback_buttons,resolve_gate_blocked",
                }

    if llm_result.get("kind") == "control" and (
        (llm_result.get("action") or "").strip().lower() == "resolved"
    ):
        db = SessionLocal()
        try:
            customer_name = _name_for_thread(db, thread_id)
        finally:
            db.close()
        copy_ctx = {"customer_name": (customer_name or "").strip()}
        if scam_context:
            resolved_ack = dynamic_copy("scam_resolved_ack", context=copy_ctx)
        elif (button_id or "").strip().lower() == "resolved" or is_strong_issue_fixed(
            user_message
        ):
            resolved_ack = dynamic_copy("resolved_ack", context=copy_ctx)
        else:
            # Farewell / soft close — do not claim the issue is fixed
            resolved_ack = dynamic_copy("resolved_ack_pause", context=copy_ctx)
        db = SessionLocal()
        try:
            new_thread_id = resolve_chatbot_thread_and_start_new(db, thread_id)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        if new_thread_id:
            db = SessionLocal()
            try:
                snap = _prior_thread_snapshot(db, new_thread_id)
                welcome_text = (
                    (snap.get("prior_welcome_blend") or "").strip() or welcome_text
                )
            finally:
                db.close()

    action = (llm_result.get("action") or "").strip()
    needs_feedback_copy = (
        llm_result.get("kind") == "action"
        and action == "feedback_buttons"
        and not (llm_result.get("message") or "").strip()
    )
    feedback_checkin = (
        _feedback_checkin()
        if needs_feedback_copy
        else "Please try this once and tell me what you see now."
    )

    outcome = outcome_from_llm_reply(
        llm_result,
        support_mode=support_mode,
        scam_context=bool(scam_context),
        feedback_checkin=feedback_checkin,
        resolved_ack=resolved_ack or "Happy to know this helped.",
        welcome_text=welcome_text,
        new_thread_id=new_thread_id,
        episode=episode,
    )
    outcome["thread_id"] = thread_id
    _log_timing(
        "turn_total",
        turn_t0,
        message_id=message_id,
        selection=bool(button_id),
        path=(llm_result.get("kind") if isinstance(llm_result, dict) else "unknown"),
    )
    return outcome
