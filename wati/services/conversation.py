import json
import logging
import re
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from sqlalchemy import desc

from app.models.chat.chat import Conversation, Message, Thread
from app.models.senior import Senior
from app.models.user import User
from wati.llm.rag_chain import search_support_docs

logger = logging.getLogger("wati.services.conversation")
MODEL_NAME = "gpt-4.1-mini"
SUPPORTED_PLATFORMS = ("apple", "samsung", "pixel", "oppo", "xiaomi")
# WhatsApp list row titles — whole message must match (case-insensitive) after platform pick.
PLATFORM_LIST_SELECTION_TOKENS = frozenset(SUPPORTED_PLATFORMS)
MAX_TOOL_STEPS = 2


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


CLASSIFIER_LLM = ChatOpenAI(model=MODEL_NAME, temperature=0)
MAIN_LLM = ChatOpenAI(model=MODEL_NAME, temperature=0).bind_tools(
    [
        send_mode_buttons,
        send_platform_buttons,
        send_feedback_buttons,
        search_support_docs,
        conversation_control,
    ]
)


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
    history: list[dict] | None = None,
) -> dict:
    """
    Minimal LLM intent classifier.
    Returns: {"intent": "..."}
    """
    history = history or _build_history_messages(db, thread_id, current_message_id, current_message)
    user_lines = [h.get("content", "") for h in history if h.get("role") == "user" and h.get("content")]
    history_window = user_lines[-3:]
    payload = payload or {}

    classifier_system = (
        "Classify latest user text for WhatsApp support.\n"
        "Return JSON only with key: intent.\n"
        "intent must be one of: REQUEST_HUMAN, RESOLVED, OTHER.\n"
        "REQUEST_HUMAN when user asks to talk to human/agent/support person.\n"
        "RESOLVED when user says issue is fixed/resolved/working.\n"
        "Otherwise OTHER."
    )
    classifier_input = {
        "latest_user_message": (current_message or "").strip(),
        "button_reply_id": (button_reply_id or "").strip().lower(),
        "payload_type": str(payload.get("type") or ""),
        "payload_event_type": str(payload.get("eventType") or ""),
        "recent_history": history_window,
    }

    try:
        result = CLASSIFIER_LLM.invoke(
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
    allowed_intents = {"REQUEST_HUMAN", "RESOLVED", "OTHER"}
    if intent not in allowed_intents:
        intent = "OTHER"
    return {"intent": intent}


def _user_chose_tech_support(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!? ")
    return t in {"tech", "tech help"}


def _user_chose_scam_support(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!? ")
    return t in {"scam", "scam help"}


def _is_platform_list_selection_only(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!? ")
    return t in PLATFORM_LIST_SELECTION_TOKENS


def _issue_prompt_for_platform(slug: str) -> str:
    labels = {
        "apple": "iPhone or iPad",
        "samsung": "Samsung phone",
        "pixel": "Pixel",
        "oppo": "Oppo phone",
        "xiaomi": "Xiaomi or Redmi phone",
    }
    device = labels.get((slug or "").strip().lower(), "phone")
    return f"What issue are you having with your {device}?"


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
    if re.search(r"\b(iphone|iphon|ipad|ios|apple|appple)\b", lowered):
        return "apple"
    if re.search(r"\b(samsung|samsng|galaxy)\b", lowered):
        return "samsung"
    if re.search(r"\b(pixel|google)\b", lowered):
        return "pixel"
    if re.search(r"\b(oppo|oppoo)\b", lowered):
        return "oppo"
    if re.search(r"\b(xiaomi|xiomi|redmi|poco)\b", lowered):
        return "xiaomi"
    return ""


def _infer_os_version(text: str) -> str:
    lowered = (text or "").lower()
    patterns = [
        r"\bios\s*\d+(?:\.\d+)?\b",
        r"\bipados\s*\d+(?:\.\d+)?\b",
        r"\bandroid\s*\d+(?:\.\d+)?\b",
        r"\bhyperos\s*\d+(?:\.\d+)?\b",
        r"\bcoloros\s*\d+(?:\.\d+)?\b",
        r"\bone\s*ui\s*\d+(?:\.\d+)?\b",
    ]
    for pat in patterns:
        m = re.search(pat, lowered, flags=re.IGNORECASE)
        if m:
            return " ".join(m.group(0).split()).lower()
    return ""


def _infer_device_model(text: str, platform: str) -> str:
    """Extract Samsung model hints when present; keep empty for other platforms."""
    if (platform or "").strip().lower() != "samsung":
        return ""

    lowered = (text or "").lower()
    galaxy = re.search(r"\b(galaxy\s+[a-z0-9\-\+\s]{1,30})\b", lowered, flags=re.IGNORECASE)
    if galaxy:
        return " ".join(galaxy.group(1).split()).lower()

    short_model = re.search(
        r"\b([aszmf]\d{1,3}\s*(?:ultra|plus|pro|fe)?)\b",
        lowered,
        flags=re.IGNORECASE,
    )
    if short_model:
        return " ".join(short_model.group(1).split()).lower()
    return ""


def _map_retrieval_os_and_version(platform: str, known_os_version: str, known_model: str) -> tuple[str, str]:
    """
    Map user context to retrieval-oriented os/version hints.
    We keep backward compatibility by passing version via os_version tool arg.
    """
    p = (platform or "").strip().lower()
    osv = (known_os_version or "").strip().lower()
    model = (known_model or "").strip().lower()

    if p == "samsung":
        return "galaxy", model
    if p == "oppo":
        return "coloros", re.sub(r"^coloros\s*", "", osv).strip()
    if p == "xiaomi":
        return "hyperos", re.sub(r"^hyperos\s*", "", osv).strip()
    if p == "pixel":
        return "android", re.sub(r"^android\s*", "", osv).strip()
    if p == "apple":
        if osv.startswith("ipados"):
            return "ipados", re.sub(r"^ipados\s*", "", osv).strip()
        if osv.startswith("ios"):
            return "ios", re.sub(r"^ios\s*", "", osv).strip()
    return "", ""


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


def _name_for_thread(db, thread_id: int) -> str:
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if not thread:
        return ""
    conv = db.query(Conversation).filter(Conversation.id == thread.conversation_id).first()
    if not conv or not conv.senior_id:
        return ""
    senior = db.query(Senior).filter(Senior.id == conv.senior_id).first()
    if not senior or not senior.user_id:
        return ""
    user = db.query(User).filter(User.id == senior.user_id).first()
    return (user.first_name or "").strip() if user else ""


def _with_name(text: str, name: str) -> str:
    msg = (text or "").strip()
    nm = (name or "").strip()
    if not msg or not nm:
        return msg
    low = msg.lower()
    tag = f"{nm.lower()} ji"
    if tag in low or nm.lower() in low:
        return msg
    return f"{nm} ji, {msg}"


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

    # OS version/model: newest user-provided technical identifier.
    known_os_version = ""
    known_model = ""
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        if not content:
            continue
        if not known_os_version:
            ov = _infer_os_version(content)
            if ov:
                known_os_version = ov
        if known_platform and not known_model:
            model = _infer_device_model(content, known_platform)
            if model:
                known_model = model
        if known_os_version and known_model:
            break

    # Issue: newest substantive user line (do not keyword-filter greetings here; LLM handles ambiguity).
    latest_issue = ""
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        if not content:
            continue
        latest_issue = content
        break

    return {
        "known_platform": known_platform,
        "known_os_version": known_os_version,
        "known_model": known_model,
        "latest_issue": latest_issue,
    }


async def generate_wati_reply(
    db,
    thread_id: int,
    current_message_id: int,
    current_message: str,
    turn_meta: dict | None = None,
    history: list[dict] | None = None,
) -> dict:
    history = history or _build_history_messages(db, thread_id, current_message_id, current_message)
    runtime_context = _infer_runtime_context(history)
    customer_name = _name_for_thread(db, thread_id)
    support_mode = _infer_support_mode(history)
    turn_meta = turn_meta or {}
    runtime_context["unresolved_signal_current_turn"] = bool(
        turn_meta.get("unresolved_signal_current_turn")
    )
    runtime_context["unresolved_rounds"] = int(turn_meta.get("unresolved_rounds") or 0)
    runtime_context["button_reply_id"] = (
        str(turn_meta.get("button_reply_id") or "").strip().lower()
    )

    if support_mode == "tech" and _is_platform_list_selection_only(current_message):
        slug = _infer_platform(current_message)
        if slug in SUPPORTED_PLATFORMS:
            return {
                "kind": "text",
                "message": _with_name(_issue_prompt_for_platform(slug), customer_name),
                "message_source": "",
                "confidence_score": None,
            }

    messages = _to_langchain_messages(history)
    messages.insert(
        1,
        SystemMessage(
            content=(
                "<runtime_context>\n"
                f"support_mode: {support_mode or 'none'}\n"
                f"known_platform: {runtime_context.get('known_platform') or 'unknown'}\n"
                f"known_os_version: {runtime_context.get('known_os_version') or 'unknown'}\n"
                f"known_model: {runtime_context.get('known_model') or 'unknown'}\n"
                f"latest_issue: {runtime_context.get('latest_issue') or 'unknown'}\n"
                f"customer_name: {customer_name or 'unknown'}\n"
                f"button_reply_id: {runtime_context.get('button_reply_id') or 'none'}\n"
                f"unresolved_signal_current_turn: {runtime_context.get('unresolved_signal_current_turn')}\n"
                f"unresolved_rounds: {runtime_context.get('unresolved_rounds')}\n"
                "If customer_name is present, greet/address the user naturally as '<customer_name> ji' where appropriate.\n"
                "When unresolved_signal_current_turn is true: follow system prompt PRECEDENCE and RULE 5 — "
                "one diagnostic question only this turn; do not call search_support_docs or send_feedback_buttons "
                "unless latest_issue already contains concrete new failure detail.\n"
                "Decide follow-up dynamically from the latest user message and context.\n"
                "If issue is clear and not blocked by PRECEDENCE, call search_support_docs immediately.\n"
                "If issue is unclear or user says still stuck (and PRECEDENCE applies), ask exactly one short follow-up question.\n"
                "Do not provide troubleshooting steps while asking follow-up.\n"
                "Do not provide lists/options in follow-up questions.\n"
                "OS/model are optional refinements; never block on missing OS/model.\n"
                "Do not repeat the same follow-up wording.\n"
                "</runtime_context>"
            )
        ),
    )
    retrieval_sources: list[str] = []
    retrieval_confidence = 0.0
    had_search_support_this_turn = False

    for _ in range(MAX_TOOL_STEPS):
        ai_message = MAIN_LLM.invoke(messages)
        tool_calls = getattr(ai_message, "tool_calls", None) or []

        if not tool_calls:
            text = (getattr(ai_message, "content", "") or "").strip()
            if had_search_support_this_turn and text and _looks_like_troubleshooting_steps(text):
                return {
                    "kind": "action",
                    "action": "feedback_buttons",
                    "message": _with_name(text, customer_name),
                    "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                    "confidence_score": retrieval_confidence,
                }
            return {
                "kind": "text",
                "message": _with_name(text, customer_name),
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
                known_os_version = (runtime_context.get("known_os_version") or "").strip().lower()
                known_model = (runtime_context.get("known_model") or "").strip().lower()
                retrieval_os, retrieval_version = _map_retrieval_os_and_version(
                    platform, known_os_version, known_model
                )
                if not str(args.get("os_version") or "").strip() and retrieval_version:
                    args["os_version"] = retrieval_version
                base_query = str(args.get("user_query") or current_message or "").strip()
                hint_lines = []
                if retrieval_os:
                    hint_lines.append(f"os_family: {retrieval_os}")
                if retrieval_version:
                    hint_lines.append(f"os_version: {retrieval_version}")
                if known_model and platform == "samsung":
                    hint_lines.append(f"device_model: {known_model}")
                if hint_lines:
                    args["user_query"] = f"{base_query}\n" + "\n".join(hint_lines)
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
                    "message": _with_name((args.get("message") or "").strip(), customer_name),
                    "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                    "confidence_score": retrieval_confidence,
                }

            if tool_name == "send_platform_buttons":
                msg = (args.get("message") or "").strip()
                return {
                    "kind": "action",
                    "action": "platform_buttons",
                    "message": _with_name(msg, customer_name),
                    "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                    "confidence_score": retrieval_confidence,
                }

            if tool_name == "send_feedback_buttons":
                msg = (args.get("message") or "").strip()
                return {
                    "kind": "action",
                    "action": "feedback_buttons",
                    "message": _with_name(msg, customer_name),
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
                "message": _with_name(text, customer_name),
                "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                "confidence_score": retrieval_confidence,
            }
        return {
            "kind": "text",
            "message": _with_name(text, customer_name),
            "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
            "confidence_score": retrieval_confidence,
        }

    return {
        "kind": "text",
        "message": _with_name("Could you share a little more detail about the issue?", customer_name),
        "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
        "confidence_score": retrieval_confidence,
    }
