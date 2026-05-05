import json
import logging
import re
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from sqlalchemy import desc

from app.models.chat.chat import Message
from wati.llm.rag_chain import search_support_docs

logger = logging.getLogger("wati.services.conversation")
MODEL_NAME = "gpt-4.1-mini"
SUPPORTED_PLATFORMS = ("apple", "samsung", "pixel", "oppo", "xiaomi")
MAX_TOOL_STEPS = 3


@tool("send_mode_buttons")
def send_mode_buttons(message: str):
    """Return intent to show mode buttons with dynamic LLM message."""
    return {"action": "mode_buttons", "message": (message or "").strip()}


@tool("send_platform_buttons")
def send_platform_buttons(message: str):
    """Return intent to show a WhatsApp list picker for phone brand with dynamic LLM message."""
    return {"action": "platform_buttons", "message": (message or "").strip()}


@tool("send_feedback_buttons")
def send_feedback_buttons(message: str):
    """Return intent to show resolved/still-stuck feedback buttons."""
    return {"action": "feedback_buttons", "message": (message or "").strip()}


@tool("conversation_control")
def conversation_control(action: str):
    """Return conversation control action."""
    return {"action": (action or "").strip()}


def _load_system_prompt() -> str:
    system_prompt_path = Path(__file__).resolve().parents[1] / "llm" / "systemprompt.txt"
    if system_prompt_path.exists():
        return system_prompt_path.read_text(encoding="utf-8").strip()
    return "You are a smartphone support assistant for WhatsApp."


def _build_history_messages(db, thread_id: int, current_message_id: int, current_user_message: str):
    messages = (
        db.query(Message)
        .filter(
            Message.thread_id == thread_id,
            Message.is_deleted.is_(False),
            Message.id != current_message_id,
        )
        .order_by(desc(Message.created))
        .limit(10)
        .all()
    )
    messages = list(reversed(messages))

    history = []
    for msg in messages:
        user_text = (msg.user_message or "").strip()
        bot_text = (msg.bot_response or "").strip()
        if user_text:
            history.append({"role": "user", "content": user_text})
        if bot_text:
            history.append({"role": "assistant", "content": bot_text})

    history.append({"role": "user", "content": (current_user_message or "").strip()})
    return history


def _to_langchain_messages(history: list[dict]):
    messages = [SystemMessage(content=_load_system_prompt())]
    for item in history:
        role = item.get("role")
        content = (item.get("content") or "").strip()
        if not content:
            continue
        if role == "assistant":
            messages.append(AIMessage(content=content))
        else:
            messages.append(HumanMessage(content=content))
    return messages


def _extract_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def classify_wati_turn_intent(
    db,
    thread_id: int,
    current_message_id: int,
    current_message: str,
    payload: dict | None = None,
    button_reply_id: str | None = None,
) -> dict:
    """
    LLM-only turn classifier.
    Returns: {"intent": "...", "next_action": "...", "confidence": float, "reason": str}
    """
    history = _build_history_messages(db, thread_id, current_message_id, current_message)
    history_window = history[-10:]
    payload = payload or {}

    classifier_system = (
        "You are a strict WhatsApp support intent classifier.\n"
        "Classify the latest user turn using full conversation context.\n"
        "Treat button presses and typed user messages as equivalent signals.\n"
        "Use this handoff policy:\n"
        "1) If the user explicitly asks for a human representative/operator/agent at any point, choose REQUEST_HUMAN and set next_action to ESCALATE_TO_HUMAN.\n"
        "2) Treat 'Still Stuck' button taps and negative free-text as equivalent unresolved signals.\n"
        "3) If unresolved signals have repeated for two rounds after troubleshooting, set intent to REQUEST_HUMAN and next_action to ESCALATE_TO_HUMAN.\n"
        "4) If unresolved but below escalation threshold, set next_action to SEND_REPLY.\n"
        "Output valid JSON only with keys: intent, next_action, confidence, reason.\n"
        "intent must be one of: RESOLVED, NOT_RESOLVED, REQUEST_HUMAN, CONTINUE_TROUBLESHOOTING, OTHER.\n"
        "next_action must be one of: CLOSE_THREAD, ESCALATE_TO_HUMAN, SEND_FEEDBACK_BUTTONS, SEND_REPLY.\n"
        "confidence must be a number between 0 and 1.\n"
        "reason must be one short sentence."
    )
    classifier_input = {
        "latest_user_message": (current_message or "").strip(),
        "button_reply_id": (button_reply_id or "").strip().lower(),
        "payload_type": str(payload.get("type") or ""),
        "payload_event_type": str(payload.get("eventType") or ""),
        "recent_history": history_window,
    }

    llm = ChatOpenAI(model=MODEL_NAME, temperature=0)
    try:
        result = llm.invoke(
            [
                SystemMessage(content=classifier_system),
                HumanMessage(content=json.dumps(classifier_input, ensure_ascii=False)),
            ]
        )
        parsed = _extract_json_object(getattr(result, "content", "") or "")
    except Exception:
        logger.exception("Intent classifier invocation failed")
        parsed = {}

    intent = str(parsed.get("intent") or "").strip().upper()
    allowed_intents = {
        "RESOLVED",
        "NOT_RESOLVED",
        "REQUEST_HUMAN",
        "CONTINUE_TROUBLESHOOTING",
        "OTHER",
    }
    if intent not in allowed_intents:
        intent = "CONTINUE_TROUBLESHOOTING"

    next_action = str(parsed.get("next_action") or "").strip().upper()
    allowed_actions = {
        "CLOSE_THREAD",
        "ESCALATE_TO_HUMAN",
        "SEND_FEEDBACK_BUTTONS",
        "SEND_REPLY",
    }
    if next_action not in allowed_actions:
        next_action = "SEND_REPLY"

    confidence = parsed.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = 0.0

    reason = str(parsed.get("reason") or "").strip()
    return {
        "intent": intent,
        "next_action": next_action,
        "confidence": float(confidence),
        "reason": reason,
    }


def _user_chose_tech_support(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!? ")
    return t in {"tech", "tech help"}


def _user_chose_scam_support(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!? ")
    return t in {"scam", "scam help"}


def _infer_support_mode(history: list[dict]) -> str:
    """Last explicit mode from WhatsApp quick-reply titles (tech / scam)."""
    mode = ""
    for item in history:
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        if _user_chose_tech_support(content):
            mode = "tech"
        elif _user_chose_scam_support(content):
            mode = "scam"
    return mode


def _infer_platform(text: str) -> str:
    lowered = (text or "").lower()
    if any(token in lowered for token in ("iphone", "ipad", "ios", "apple")):
        return "apple"
    if any(token in lowered for token in ("samsung", "galaxy")):
        return "samsung"
    if any(token in lowered for token in ("pixel", "google")):
        return "pixel"
    if "oppo" in lowered:
        return "oppo"
    if any(token in lowered for token in ("xiaomi", "redmi", "poco")):
        return "xiaomi"
    return ""


def _looks_like_troubleshooting_steps(text: str) -> bool:
    """True when assistant text looks like numbered fix steps (after retrieval we force feedback buttons)."""
    if not text or len(text) < 40:
        return False
    lowered = text.lower()
    if "step 1" in lowered or "step 1:" in lowered:
        return True
    if re.search(r"(?m)^\s*\d+\.\s+\S", text):
        return True
    return False


def _infer_runtime_context(history: list[dict]) -> dict:
    # Platform: most recent user message that mentions a supported device (scan newest first).
    known_platform = ""
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        p = _infer_platform(content)
        if p:
            known_platform = p
            break

    # Issue: newest substantive user line (do not keyword-filter greetings here; LLM handles ambiguity).
    latest_issue = ""
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        if not content or len(content) < 10:
            continue
        latest_issue = content
        break

    return {
        "known_platform": known_platform,
        "latest_issue": latest_issue,
    }


async def generate_wati_reply(db, thread_id: int, current_message_id: int, current_message: str) -> dict:
    history = _build_history_messages(db, thread_id, current_message_id, current_message)
    runtime_context = _infer_runtime_context(history)
    support_mode = _infer_support_mode(history)

    # Deterministic: after Tech Help, WhatsApp must show the phone-brand list, not mode buttons again.
    if support_mode == "tech" and not (runtime_context.get("known_platform") or "").strip():
        return {
            "kind": "action",
            "action": "platform_buttons",
            "message": (
                "Which phone brand do you need help with? Tap \"Choose phone\", then pick your brand."
            ),
            "message_source": "",
            "confidence_score": 0.0,
        }

    llm = ChatOpenAI(model=MODEL_NAME, temperature=0).bind_tools(
        [
            send_mode_buttons,
            send_platform_buttons,
            send_feedback_buttons,
            search_support_docs,
            conversation_control,
        ]
    )

    messages = _to_langchain_messages(history)
    messages.insert(
        1,
        SystemMessage(
            content=(
                "<runtime_context>\n"
                f"support_mode: {support_mode or 'none'}\n"
                f"known_platform: {runtime_context.get('known_platform') or 'unknown'}\n"
                f"latest_issue: {runtime_context.get('latest_issue') or 'unknown'}\n"
                "If known_platform is present, do not ask for phone platform again.\n"
                "If support_mode is tech and known_platform is unknown, call send_platform_buttons only; "
                "never send_mode_buttons.\n"
                "If support_mode is scam, do not send_platform_buttons unless the user moves to smartphone tech help.\n"
                "</runtime_context>"
            )
        ),
    )
    retrieval_sources: list[str] = []
    retrieval_confidence = 0.0
    had_search_support_this_turn = False

    for _ in range(MAX_TOOL_STEPS):
        ai_message = llm.invoke(messages)
        tool_calls = getattr(ai_message, "tool_calls", None) or []

        if not tool_calls:
            text = (getattr(ai_message, "content", "") or "").strip()
            if had_search_support_this_turn and text and _looks_like_troubleshooting_steps(text):
                return {
                    "kind": "action",
                    "action": "feedback_buttons",
                    "message": text,
                    "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                    "confidence_score": retrieval_confidence,
                }
            return {
                "kind": "text",
                "message": text,
                "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                "confidence_score": retrieval_confidence,
            }

        messages.append(ai_message)
        search_tool_used = False

        for tool_call in tool_calls:
            tool_name = tool_call.get("name")
            args = tool_call.get("args", {}) or {}
            tool_call_id = tool_call.get("id", "tool-call")

            if tool_name == "search_support_docs":
                platform = str(args.get("platform") or "").strip().lower()
                if not platform:
                    platform = (runtime_context.get("known_platform") or "").strip().lower()
                if not platform or platform not in SUPPORTED_PLATFORMS:
                    return {
                        "kind": "action",
                        "action": "platform_buttons",
                        "message": "Which phone are you using?",
                    }
                args["platform"] = platform
                try:
                    result = search_support_docs.invoke(args)
                except Exception:
                    logger.exception("search_support_docs invocation failed")
                    return {
                        "kind": "action",
                        "action": "platform_buttons",
                        "message": "Which phone are you using?",
                    }
                messages.append(
                    ToolMessage(
                        content=(result.get("context", "") or ""),
                        tool_call_id=tool_call_id,
                        name="search_support_docs",
                    )
                )
                sources = result.get("sources") or []
                if isinstance(sources, list):
                    retrieval_sources = [str(item) for item in sources if str(item).strip()]
                confidence = result.get("confidence_score")
                if isinstance(confidence, (int, float)):
                    retrieval_confidence = float(confidence)
                search_tool_used = True
                had_search_support_this_turn = True
                continue

            if tool_name == "send_mode_buttons":
                return {
                    "kind": "action",
                    "action": "mode_buttons",
                    "message": (args.get("message") or "").strip(),
                    "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                    "confidence_score": retrieval_confidence,
                }

            if tool_name == "send_platform_buttons":
                return {
                    "kind": "action",
                    "action": "platform_buttons",
                    "message": (args.get("message") or "").strip(),
                    "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                    "confidence_score": retrieval_confidence,
                }

            if tool_name == "send_feedback_buttons":
                return {
                    "kind": "action",
                    "action": "feedback_buttons",
                    "message": (args.get("message") or "").strip(),
                    "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                    "confidence_score": retrieval_confidence,
                }

            if tool_name == "conversation_control":
                return {"kind": "control", "action": (args.get("action") or "").strip()}

        if search_tool_used:
            continue

        text = (getattr(ai_message, "content", "") or "").strip()
        if had_search_support_this_turn and text and _looks_like_troubleshooting_steps(text):
            return {
                "kind": "action",
                "action": "feedback_buttons",
                "message": text,
                "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                "confidence_score": retrieval_confidence,
            }
        return {
            "kind": "text",
            "message": text,
            "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
            "confidence_score": retrieval_confidence,
        }

    return {
        "kind": "text",
        "message": "Could you share a little more detail about the issue?",
        "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
        "confidence_score": retrieval_confidence,
    }
