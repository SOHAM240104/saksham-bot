import json
import logging
import re
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from sqlalchemy import desc, func

from app.models.chat.chat import Conversation, Message, Thread
from app.models.chatbot.context import PlatformModel
from app.models.senior import Senior
from app.models.user import User
from wati.llm.rag_chain import search_scam_kb, search_support_docs
from wati.services.dynamic_copy import dynamic_copy
from wati.services.supported_refinements import check_platform_refinement
from wati.services.unresolved_flow import (
    UNRESOLVED_PHASE_DIAGNOSTIC,
    UNRESOLVED_PHASE_REFINED_RETRY,
    retrieve_for_refined_unresolved_turn,
)
from wati.settings import settings

logger = logging.getLogger("wati.services.conversation")
MODEL_NAME = "gpt-4.1-mini"
CLASSIFIER_MODEL_NAME = "gpt-4o-mini"
MAX_OUTPUT_TOKENS = 1000
SUPPORTED_PLATFORMS = ("apple", "samsung", "pixel", "oppo", "xiaomi")
# WhatsApp list row titles — whole message must match (case-insensitive) after platform pick.
PLATFORM_LIST_SELECTION_TOKENS = frozenset(SUPPORTED_PLATFORMS)
_PLATFORM_LIST_TEXT_ALIASES = frozenset(
    {
        "google pixel",
        "iphone / ipad",
        "apple iphone / ipad",
        "redmi / poco",
        "galaxy",
        "samsung galaxy",
    }
)
_PLATFORM_ISSUE_HINTS = re.compile(
    r"\b(help|issue|problem|stuck|error|how|wallpaper|battery|wifi|cannot|can't|not working)\b",
    re.IGNORECASE,
)


def _is_platform_only_user_message(text: str, button_reply_id: str = "") -> bool:
    """True when the user message is only a phone-brand list pick (not a tech issue)."""
    btn = (button_reply_id or "").strip().lower()
    if btn in PLATFORM_LIST_SELECTION_TOKENS:
        return True
    t = (text or "").strip().lower().rstrip(".!? ")
    if t in PLATFORM_LIST_SELECTION_TOKENS or t in _PLATFORM_LIST_TEXT_ALIASES:
        return True
    slug = _platform_slug_from_turn(text, button_reply_id)
    if slug not in SUPPORTED_PLATFORMS:
        return False
    if _PLATFORM_ISSUE_HINTS.search(t):
        return False
    # WhatsApp list rows are short brand labels (e.g. "Apple iPhone / iPad").
    return len(t.split()) <= 6


MAX_TOOL_STEPS = 2

_SCAM_ENTRY_TOKENS = frozenset({"scam", "scam help"})

_SCAM_KB_TOOL_PREFIX = (
    "Use ONLY the knowledge base excerpts below. Do NOT add facts, steps, or warnings "
    "not present in the excerpts. Use numbered steps as in the excerpts (light paraphrase "
    "for warmth only).\n\n"
    "--- KNOWLEDGE BASE EXCERPTS ---\n"
)


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


CLASSIFIER_LLM = ChatOpenAI(
    model=CLASSIFIER_MODEL_NAME, temperature=0, max_tokens=MAX_OUTPUT_TOKENS
)
MAIN_LLM = ChatOpenAI(
    model=MODEL_NAME, temperature=0, max_tokens=MAX_OUTPUT_TOKENS
).bind_tools(
    [
        send_mode_buttons,
        send_platform_buttons,
        send_feedback_buttons,
        search_support_docs,
        search_scam_kb,
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
            history.append(
                {
                    "role": "user",
                    "content": user_text,
                    "message_source": (msg.message_source or "").strip(),
                }
            )
        if bot_text:
            history.append(
                {
                    "role": "assistant",
                    "content": bot_text,
                    "message_source": (msg.message_source or "").strip(),
                }
            )

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
    LLM turn-state classifier used by webhook policy gates.
    Returns normalized semantic signals plus intent.
    """
    history = history or _build_history_messages(db, thread_id, current_message_id, current_message)
    history_window = history[-12:]
    payload = payload or {}

    classifier_system = (
        f"{_load_system_prompt()}\n\n"
        "<classification_task>\n"
        "Based on the system policy above, classify latest turn state.\n"
        "Return JSON only with keys:\n"
        "intent, active_branch, same_issue_as_previous, is_unresolved_followup, issue_signature, "
        "issue_followup_depth.\n"
        "active_branch must be one of: scam, tech, ambiguous — judge from FULL recent_history + latest_user_message (meaning, not keywords).\n"
        "  scam: fraud/safety thread (OTP, bank, threats, fake apps for theft, parcel/digital arrest, romance, payment) OR assistant already gave scam educate/URGENT/1930/bank help in this thread.\n"
        "  tech: phone settings, overheating, wallpaper, apps, device how-tos OR assistant gave troubleshooting/platform steps for a device issue.\n"
        "  ambiguous: greeting-only or unclear; not a clear scam or tech story yet.\n"
        "is_unresolved_followup=true ONLY for tech troubleshooting (still stuck / not working on phone issue) — always false when active_branch is scam.\n"
        "Treat short unresolved-negative continuations like 'still stuck', 'not resolved', 'same issue', 'not working', 'didn't work' (tech phone issues only) as is_unresolved_followup=true.\n"
        "intent must be one of: REQUEST_HUMAN, RESOLVED, OTHER.\n"
        "intent RESOLVED only when the user clearly ends the CURRENT help arc (thanks, all done, sorted, bye) — NOT for scam mid-flow replies like bank name, 'no I didn't', 'done' after 1930 offer, or 'what now'.\n"
        "When active_branch is scam and scam help is still in progress → intent must be OTHER (never RESOLVED).\n"
        "When user asks for human/agent → intent REQUEST_HUMAN.\n"
        "issue_signature must be short snake_case.\n"
        "issue_followup_depth must be integer >= 1.\n"
        "For tech SAME ISSUE unresolved flow only, escalation threshold is depth 3.\n"
        "If latest turn is a NEW issue/topic switch, set same_issue_as_previous=false and issue_followup_depth=1.\n"
        "No prose, no markdown.\n"
        "</classification_task>"
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
    same_issue = bool(parsed.get("same_issue_as_previous"))
    unresolved_followup = bool(parsed.get("is_unresolved_followup"))
    issue_signature = re.sub(
        r"[^a-z0-9_]+",
        "_",
        str(parsed.get("issue_signature") or "").strip().lower(),
    ).strip("_")
    if not issue_signature:
        issue_signature = "general_issue"
    depth_raw = parsed.get("issue_followup_depth")
    try:
        issue_followup_depth = int(depth_raw)
    except Exception:
        issue_followup_depth = 1
    if issue_followup_depth < 1:
        issue_followup_depth = 1

    active_branch = str(parsed.get("active_branch") or "").strip().lower()
    if active_branch not in {"scam", "tech", "ambiguous"}:
        active_branch = "ambiguous"
    if active_branch == "scam" and intent == "RESOLVED":
        intent = "OTHER"
    if active_branch == "scam":
        unresolved_followup = False

    return {
        "intent": intent,
        "active_branch": active_branch,
        "same_issue_as_previous": same_issue,
        "is_unresolved_followup": unresolved_followup,
        "issue_signature": issue_signature,
        "issue_followup_depth": issue_followup_depth,
    }


def _user_chose_tech_support(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!? ")
    return t in {"tech", "tech help"}


def _user_chose_scam_support(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!? ")
    return t in {"scam", "scam help"}


def _is_on_tech_path(
    support_mode: str,
    current_message: str,
    button_reply_id: str = "",
    *,
    active_branch: str = "",
) -> bool:
    """True when the user is on (or just chose) Tech Help — never show scam-only UI."""
    btn = (button_reply_id or "").strip().lower()
    branch = (active_branch or "").strip().lower()
    if btn == "tech" or _user_chose_tech_support(current_message):
        return True
    if (support_mode or "").strip().lower() == "tech":
        return True
    if branch == "tech":
        return True
    return False




#branch change short replies
_BRANCH_CHANGE_SHORT_REPLIES = frozenset(
    {"yes", "y", "yeah", "yep", "ha", "haan", "han", "ji", "ok", "okay", "sure", "no", "n", "nope", "nah", "nahi", "na", "not now", "continue"}
)
_SWITCH_YES = frozenset(
    {"yes", "y", "yeah", "yep", "ha", "haan", "han", "ji", "ok", "okay", "sure"}
)
_SWITCH_NO = frozenset(
    {"no", "n", "nope", "nah", "nahi", "na", "not now", "continue"}
)


def _user_switch_reply(text: str) -> str | None:
    t = (text or "").strip().lower().rstrip(".!? ")
    if t in _SWITCH_YES:
        return "yes"
    if t in _SWITCH_NO:
        return "no"
    return None


def _assistant_offered_branch_change(history: list[dict], target: str) -> bool:
    """True if the last assistant turn offered to change tech vs scam branch (plain text)."""
    target = (target or "").strip().lower()
    for item in reversed(history[:-1]):
        if item.get("role") != "assistant":
            continue
        content = (item.get("content") or "").lower()
        offered = (
            "yes or no" in content
            or "reply yes" in content
            or "if yes" in content
            or "please confirm" in content
            or "would you like" in content
            or "do you want" in content
            or "shall we" in content
            or "or continue" in content
            or "or stay" in content
            or "switch to" in content
            or "stay on" in content
        )
        if not offered:
            return False
        if target == "scam":
            return "scam" in content
        if target == "tech":
            return "tech" in content or "phone" in content
        return False
    return False


def _is_branch_change_short_reply(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!? ")
    return t in _BRANCH_CHANGE_SHORT_REPLIES


def resolve_scam_context_from_turn(
    history: list[dict],
    current_message: str,
    button_reply_id: str = "",
    support_mode: str | None = None,
    intent_result: dict | None = None,
) -> tuple[str, bool]:
    """Return (support_mode, scam_mode). Prompt-led branch via classifier + explicit taps."""
    intent_result = intent_result or {}
    branch = (intent_result.get("active_branch") or "").strip().lower()
    mode = (support_mode or "").strip().lower() or _infer_support_mode(history)
    btn = (button_reply_id or "").strip().lower()
    msg = (current_message or "").strip()
    switch = _user_switch_reply(msg)

    if _user_chose_tech_support(msg) or btn == "tech":
        mode = "tech"
    elif btn == "scam" or _user_chose_scam_support(msg):
        mode = "scam"
    elif btn in {"ios", "android"} and (mode or "").strip().lower() == "tech":
        mode = "tech"
    elif switch == "yes" and _assistant_offered_branch_change(history, "scam"):
        mode = "scam"
    elif switch == "yes" and _assistant_offered_branch_change(history, "tech"):
        mode = "tech"
    elif switch == "no":
        prior = _infer_support_mode(history)
        if prior:
            mode = prior
    elif branch in {"scam", "tech"}:
        mode = branch

    return mode, mode == "scam"


def _effective_scam_path(
    scam_mode: bool,
    *,
    had_search_scam: bool = False,
    button_reply_id: str = "",
) -> bool:
    btn = (button_reply_id or "").strip().lower()
    if scam_mode or had_search_scam:
        return True
    return btn == "scam"


def _pending_user_issue_from_history(
    history: list[dict],
    current_message: str,
    button_reply_id: str = "",
) -> str:
    """Last substantive user message before a branch-change reply (for tech/scam handoff)."""
    btn = (button_reply_id or "").strip().lower()
    skip_current = _is_branch_change_short_reply(current_message) or _user_switch_reply(current_message)
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        if not content:
            continue
        if skip_current and content == (current_message or "").strip():
            skip_current = False
            continue
        if _is_scam_entry_message(content) or _user_chose_tech_support(content):
            continue
        if _is_platform_only_user_message(content):
            continue
        if _user_switch_reply(content) or len(content) < 12:
            continue
        return content[:500]
    current = (current_message or "").strip()
    if (
        current
        and not _is_branch_change_short_reply(current)
        and not _user_switch_reply(current)
        and not _is_platform_only_user_message(current, button_reply_id)
        and len(current) >= 12
        and not _is_scam_entry_message(current)
    ):
        return current[:500]
    return ""


def _fallback_reply_text(on_scam_path: bool) -> str:
    if on_scam_path:
        return (
            "Could you tell me a bit more about what happened, "
            "so I can guide you better?"
        )
    return "Could you share a little more detail about the issue?"


def _is_scam_entry_message(text: str, button_reply_id: str = "") -> bool:
    t = (text or "").strip().lower().rstrip(".!? ")
    btn = (button_reply_id or "").strip().lower()
    return btn == "scam" or t in _SCAM_ENTRY_TOKENS


def _is_scam_os_reply(button_reply_id: str = "") -> bool:
    return (button_reply_id or "").strip().lower() in {"ios", "android"}


def _phone_os_from_message_source(message_source: str) -> str:
    for part in (message_source or "").split(","):
        source = part.strip().lower()
        if source == "scam_os:ios":
            return "ios"
        if source == "scam_os:android":
            return "android"
    return ""


def _known_scam_phone_os(
    history: list[dict],
    button_reply_id: str = "",
    *,
    stored_platform: str = "",
) -> str:
    """OS from iOS/Android taps, history scam_os:*, or inferred from stored platform."""
    if _is_scam_os_reply(button_reply_id):
        return (button_reply_id or "").strip().lower()
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        os = _phone_os_from_message_source(item.get("message_source") or "")
        if os:
            return os
    return _phone_os_from_platform_slug(stored_platform)


def _user_has_described_scam_situation(
    history: list[dict],
    current_message: str,
    button_reply_id: str = "",
) -> bool:
    """True when the user has given enough context beyond a bare Scam Help tap."""
    current = (current_message or "").strip()
    if (
        current
        and not _is_scam_entry_message(current, button_reply_id)
        and not _is_scam_os_reply(button_reply_id)
        and len(current) >= 20
    ):
        return True
    for item in history:
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        if (
            content
            and not _is_scam_entry_message(content)
            and not _phone_os_from_message_source(item.get("message_source") or "")
            and len(content) >= 20
        ):
            return True
    return False


def _format_scam_kb_tool_message(context: str) -> str:
    body = (context or "").strip()
    if not body:
        return (
            f"{_SCAM_KB_TOOL_PREFIX}"
            "(no excerpts — tell the user you lack detailed material; suggest 1930; do not invent scam details.)"
        )
    return f"{_SCAM_KB_TOOL_PREFIX}{body}\n--- END EXCERPTS ---"


def _scam_text_reply(message: str, retrieval_sources: list[str] | None = None) -> dict:
    sources = [s for s in (retrieval_sources or []) if str(s).strip()]
    message_source = "scam_flow"
    if sources:
        message_source = "scam_flow," + ",".join(sources)
    return {
        "kind": "text",
        "message": (message or "").strip(),
        "message_source": message_source,
    }


def _is_platform_list_selection_only(text: str) -> bool:
    t = (text or "").strip().lower().rstrip(".!? ")
    return t in PLATFORM_LIST_SELECTION_TOKENS


_PLATFORM_LABELS = {
    "apple": "iPhone or iPad",
    "samsung": "Samsung phone",
    "pixel": "Pixel",
    "oppo": "Oppo phone",
    "xiaomi": "Xiaomi or Redmi phone",
}


def _platform_label(slug: str) -> str:
    return _PLATFORM_LABELS.get((slug or "").strip().lower(), "phone")


def _platform_os_refinement_hint(slug: str) -> str:
    """Optional OS/model ask for Still Stuck follow-up — one clause, user may skip (RULE 5)."""
    p = (slug or "").strip().lower()
    hints = {
        "apple": "iOS version from Settings > General > About",
        "samsung": "Galaxy model and Android version from Settings > About phone",
        "pixel": "Android version from Settings > About phone",
        "oppo": "ColorOS version from Settings > About device",
        "xiaomi": "HyperOS or Android version from Settings > About phone",
    }
    return hints.get(p, "phone software version from Settings > About phone or device")


def _issue_prompt_for_platform(slug: str) -> str:
    return f"What issue are you having with your {_platform_label(slug)}?"


def _phone_os_from_platform_slug(slug: str) -> str:
    p = (slug or "").strip().lower()
    if p == "apple":
        return "ios"
    if p in {"samsung", "pixel", "oppo", "xiaomi"}:
        return "android"
    return ""


def _platform_slug_from_user_turn(text: str, button_reply_id: str = "") -> str:
    return _platform_slug_from_turn(text, button_reply_id)


def _senior_for_thread(db, thread_id: int) -> Senior | None:
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if not thread:
        return None
    conv = db.query(Conversation).filter(Conversation.id == thread.conversation_id).first()
    if not conv or not conv.senior_id:
        return None
    return db.query(Senior).filter(Senior.id == conv.senior_id).first()


def _platform_slug_from_senior(db, senior: Senior | None) -> str:
    if not senior or not senior.device_id:
        return ""
    row = (
        db.query(PlatformModel)
        .filter(PlatformModel.id == senior.device_id)
        .first()
    )
    slug = (row.identity or "").strip().lower() if row else ""
    return slug if slug in SUPPORTED_PLATFORMS else ""


def _platform_row_for_slug(db, slug: str) -> PlatformModel | None:
    normalized = (slug or "").strip().lower()
    if normalized not in SUPPORTED_PLATFORMS:
        return None
    # Supported phone brands may remain referenced while soft-deleted in admin.
    return (
        db.query(PlatformModel)
        .filter(func.lower(PlatformModel.identity) == normalized)
        .order_by(PlatformModel.is_deleted.asc(), PlatformModel.id.asc())
        .first()
    )


def save_senior_platform_from_turn(
    db, senior: Senior | None, text: str, button_reply_id: str = ""
) -> str:
    """Persist platform slug to senior.device_id; return slug or ""."""
    if not senior:
        return ""
    slug = _platform_slug_from_turn(text, button_reply_id)
    if slug not in SUPPORTED_PLATFORMS:
        return ""
    row = _platform_row_for_slug(db, slug)
    if not row:
        logger.warning("PlatformModel not found for slug=%s", slug)
        return ""
    senior.device_id = row.id
    db.flush()
    return slug


def thread_has_platform_issue_prompt(
    db, thread_id: int, *, exclude_message_id: int | None = None
) -> bool:
    query = db.query(Message).filter(
        Message.thread_id == thread_id,
        Message.is_deleted.is_(False),
        Message.bot_response.isnot(None),
    )
    if exclude_message_id:
        query = query.filter(Message.id != exclude_message_id)
    rows = query.order_by(desc(Message.created)).limit(20).all()
    for row in rows:
        if "platform_issue_prompt" in (row.message_source or ""):
            return True
    return False


def _recent_user_messages_snippet(history: list[dict], *, limit: int = 8) -> str:
    """Verbatim recent user lines for runtime_context — no issue/platform heuristics."""
    lines: list[str] = []
    for item in history:
        if item.get("role") != "user":
            continue
        content = (item.get("content") or "").strip()
        if content:
            lines.append(content[:500])
    if not lines:
        return "none"
    recent = lines[-limit:]
    return "\n".join(f"  - {line}" for line in recent)


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


def _is_platform_list_selection(text: str, button_reply_id: str = "") -> bool:
    return _is_platform_only_user_message(text, button_reply_id)


def _platform_slug_from_turn(text: str, button_reply_id: str = "") -> str:
    btn = (button_reply_id or "").strip().lower()
    if btn in SUPPORTED_PLATFORMS:
        return btn
    t = (text or "").strip().lower().rstrip(".!? ")
    if t in PLATFORM_LIST_SELECTION_TOKENS:
        return t
    slug = _infer_platform(text)
    if slug in SUPPORTED_PLATFORMS:
        return slug
    return ""


def _should_ask_tech_issue_before_retrieval(
    *,
    on_tech_path: bool,
    scam_mode: bool,
    pending_user_issue: str,
    unresolved_signal: bool,
    current_message: str,
    button_reply_id: str = "",
    user_query: str = "",
) -> str:
    """Return platform slug when brand-only — must ask issue before search_support_docs."""
    if not on_tech_path or scam_mode or unresolved_signal:
        return ""
    is_platform_turn = _is_platform_only_user_message(
        current_message, button_reply_id
    ) or _is_platform_only_user_message(user_query, "")
    if not is_platform_turn:
        return ""
    # RULE 2b: real issue already stated earlier — skip issue prompt, go to retrieval.
    if pending_user_issue and not _is_platform_only_user_message(pending_user_issue, ""):
        return ""
    slug = _platform_slug_from_turn(current_message, button_reply_id)
    if not slug and user_query:
        slug = _platform_slug_from_turn(user_query, button_reply_id)
    return slug if slug in SUPPORTED_PLATFORMS else ""


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


def refinement_platform_for_turn(db, thread_id: int, history: list[dict]) -> str:
    """Platform slug used for OS/model validation — DB device first, then chat history."""
    senior = _senior_for_thread(db, thread_id)
    slug = _platform_slug_from_senior(db, senior)
    if slug:
        return slug
    ctx = _infer_runtime_context(history)
    return (ctx.get("known_platform") or "").strip().lower()


def _is_unresolved_negative_only(text: str, button_reply_id: str = "") -> bool:
    """True when the user only signals the previous fix did not work (Still Stuck tap or equivalent)."""
    btn = (button_reply_id or "").strip().lower()
    if btn in {"not_resolved", "still_stuck"}:
        return True
    t = re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(".!? ")
    if not t:
        return False
    short_phrases = (
        "still stuck",
        "not resolved",
        "same issue",
        "not working",
        "didn't work",
        "did not work",
        "can't figure it out",
        "cannot figure it out",
        "still not working",
    )
    if t in short_phrases:
        return True
    if len(t) < 48 and any(phrase in t for phrase in short_phrases):
        return True
    return False


def _prior_still_stuck_count(history: list[dict]) -> int:
    """Count Still Stuck / not-resolved user turns before the latest message."""
    items = history[:-1] if history else []
    count = 0
    for item in items:
        if item.get("role") != "user":
            continue
        if _is_unresolved_negative_only(item.get("content") or "", ""):
            count += 1
    return count


def _last_assistant_troubleshooting_from_history(history: list[dict]) -> str:
    for item in reversed(history):
        if item.get("role") != "assistant":
            continue
        content = (item.get("content") or "").strip()
        if content and _looks_like_troubleshooting_steps(content):
            return content
    return ""


def _unsupported_refinement_word(platform: str, refinement_type: str) -> str:
    p = (platform or "").strip().lower()
    if p == "samsung" or refinement_type == "model":
        return "model"
    return "version"


def _unsupported_refinement_offer_reply(
    *,
    customer_name: str,
    platform: str,
    unsupported_label: str,
    refinement_type: str,
) -> dict:
    text = dynamic_copy(
        "unsupported_refinement_offer",
        context={
            "customer_name": (customer_name or "").strip(),
            "platform_label": _platform_label(platform),
            "platform_slug": (platform or "").strip().lower(),
            "unsupported_label": (unsupported_label or "").strip(),
            "refinement_word": _unsupported_refinement_word(platform, refinement_type),
        },
    )
    return {
        "kind": "text",
        "message": (text or "").strip(),
        "message_source": "unsupported_refinement_confirmation,dynamic_copy",
    }


def _unsupported_refinement_declined_reply(
    *,
    customer_name: str,
    platform: str,
) -> dict:
    text = dynamic_copy(
        "unsupported_refinement_declined",
        context={
            "customer_name": (customer_name or "").strip(),
            "platform_label": _platform_label(platform),
            "platform_slug": (platform or "").strip().lower(),
        },
    )
    return {
        "kind": "text",
        "message": (text or "").strip(),
        "message_source": "unsupported_refinement_declined,dynamic_copy",
    }


def _unresolved_diagnostic_reply(
    *,
    customer_name: str,
    pending_user_issue: str,
    platform: str,
    prior_steps: str = "",
) -> dict:
    issue_summary = (pending_user_issue or "your phone issue").strip()[:200]
    platform_slug = (platform or "").strip().lower()
    os_refinement_hint = _platform_os_refinement_hint(platform_slug)
    text = dynamic_copy(
        "unresolved_diagnostic",
        context={
            "customer_name": (customer_name or "").strip(),
            "issue_summary": issue_summary,
            "platform_label": _platform_label(platform_slug),
            "platform_slug": platform_slug,
            "os_refinement_hint": os_refinement_hint,
            "prior_steps_snippet": (prior_steps or "")[:400],
        },
    )
    return {
        "kind": "text",
        "message": (text or "").strip(),
        "message_source": "unresolved_diagnostic,dynamic_copy",
    }


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

    return {
        "known_platform": known_platform,
        "known_os_version": known_os_version,
        "known_model": known_model,
    }


def _build_runtime_context(db, thread_id: int, history: list[dict]) -> dict:
    chat_ctx = _infer_runtime_context(history)
    senior = _senior_for_thread(db, thread_id)
    stored_platform = _platform_slug_from_senior(db, senior)
    needs_platform_pick = not bool(senior and senior.device_id)
    retrieval_platform = stored_platform if stored_platform in SUPPORTED_PLATFORMS else ""
    known_platform = retrieval_platform or (chat_ctx.get("known_platform") or "")
    return {
        **chat_ctx,
        "known_platform": known_platform,
        "stored_platform": stored_platform,
        "needs_platform_pick": needs_platform_pick,
        "retrieval_platform": retrieval_platform,
    }


def _user_switching_platform(
    stored_platform: str,
    current_message: str,
    button_reply_id: str = "",
) -> bool:
    slug = _platform_slug_from_turn(current_message, button_reply_id)
    stored = (stored_platform or "").strip().lower()
    return bool(slug and stored and slug != stored)


def _prior_thread_snapshot(db, thread_id: int) -> dict[str, str]:
    """
    Load last resolved-thread turns plus short topic/summary for continuity hints.
    Used when fresh_chatbot_thread is true (first message on new thread after rollover).
    """
    empty: dict[str, str] = {
        "prior_issue_topic": "",
        "prior_thread_summary": "",
        "prior_turns_snippet": "",
        "prior_welcome_blend": "",
    }
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if not thread:
        return empty
    prev_thread = (
        db.query(Thread)
        .filter(
            Thread.conversation_id == thread.conversation_id,
            Thread.id != thread_id,
        )
        .order_by(desc(Thread.created))
        .first()
    )
    if not prev_thread:
        return empty
    prev_rows = (
        db.query(Message)
        .filter(
            Message.thread_id == prev_thread.id,
            Message.is_deleted.is_(False),
        )
        .order_by(desc(Message.created))
        .limit(5)
        .all()
    )
    prev_rows = list(reversed(prev_rows))
    previous_turns: list[dict] = []
    lines_for_snippet: list[str] = []
    for row in prev_rows:
        user_text = (row.user_message or "").strip()
        bot_text = (row.bot_response or "").strip()
        if user_text:
            previous_turns.append({"role": "user", "content": user_text})
            lines_for_snippet.append(f"User: {user_text[:200]}")
        if bot_text:
            previous_turns.append({"role": "assistant", "content": bot_text})
            lines_for_snippet.append(f"Assistant: {bot_text[:200]}")
    if not previous_turns:
        return empty
    topic_raw = dynamic_copy("thread_topic", context={"previous_turns": previous_turns})
    topic_raw = (topic_raw or "").strip()
    normalized = ""
    if topic_raw:
        normalized = " ".join(topic_raw.split())
        normalized = re.sub(
            r"\b(resolved|fixed|solved|closed|completed|done)\b",
            "",
            normalized,
            flags=re.IGNORECASE,
        ).strip(" .,-")
        if len(normalized) > 90:
            normalized = normalized[:87].rstrip() + "..."
    snippet = " | ".join(lines_for_snippet)
    snippet = snippet.replace("<", " ").replace(">", " ")
    if len(snippet) > 900:
        snippet = snippet[:897] + "..."

    prior_welcome_blend = ""
    if normalized:
        blended = dynamic_copy(
            "welcome_back_blended",
            context={
                "issue_topic": normalized,
                "previous_turns": previous_turns,
            },
        )
        blended = (blended or "").strip()
        blended = re.sub(
            r"\b(not resolved|unresolved|not fixed|failed|did not work|didn't work)\b",
            "all good",
            blended,
            flags=re.IGNORECASE,
        )
        prior_welcome_blend = blended.strip()
    else:
        prior_welcome_blend = (
            dynamic_copy("welcome_back_context", context={"previous_turns": previous_turns})
            or ""
        ).strip()

    return {
        "prior_issue_topic": normalized or "",
        # Same thread_topic LLM output as prior_issue_topic (raw); normalized is cleaned for blending.
        "prior_thread_summary": topic_raw,
        "prior_turns_snippet": snippet,
        "prior_welcome_blend": prior_welcome_blend,
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
    runtime_context = _build_runtime_context(db, thread_id, history)
    customer_name = _name_for_thread(db, thread_id)
    turn_meta = turn_meta or {}
    button_reply_id = (turn_meta.get("button_reply_id") or "").strip().lower()
    support_mode, scam_mode = resolve_scam_context_from_turn(
        history,
        current_message,
        button_reply_id,
        intent_result={
            "active_branch": (turn_meta.get("active_branch") or "").strip().lower(),
        },
    )
    runtime_context["unresolved_signal_current_turn"] = bool(
        turn_meta.get("unresolved_signal_current_turn")
    )
    runtime_context["unresolved_rounds"] = int(turn_meta.get("unresolved_rounds") or 0)
    if scam_mode:
        runtime_context["unresolved_signal_current_turn"] = False
        runtime_context["unresolved_rounds"] = 0
    runtime_context["button_reply_id"] = (
        str(turn_meta.get("button_reply_id") or "").strip().lower()
    )
    fresh_chatbot_thread = bool(turn_meta.get("fresh_chatbot_thread"))
    post_resolve_welcome_sent = bool(turn_meta.get("post_resolve_welcome_sent"))
    prior_issue_topic = ""
    prior_thread_summary = ""
    prior_turns_snippet = ""
    prior_welcome_blend = ""
    if fresh_chatbot_thread:
        snap = _prior_thread_snapshot(db, thread_id)
        prior_issue_topic = snap.get("prior_issue_topic") or ""
        prior_thread_summary = snap.get("prior_thread_summary") or ""
        prior_turns_snippet = snap.get("prior_turns_snippet") or ""
        prior_welcome_blend = snap.get("prior_welcome_blend") or ""

    bank_helpline_url = (settings.BANK_HELPLINE_URL or "").strip()
    pending_user_issue = _pending_user_issue_from_history(
        history, current_message, button_reply_id
    )
    recent_user_messages = _recent_user_messages_snippet(history)
    active_branch = (turn_meta.get("active_branch") or "").strip().lower()
    on_tech_path = _is_on_tech_path(
        support_mode,
        current_message,
        button_reply_id,
        active_branch=active_branch,
    )
    known_phone_os = ""
    stored_platform = (runtime_context.get("stored_platform") or "").strip().lower()
    if scam_mode and not on_tech_path:
        known_phone_os = _known_scam_phone_os(
            history,
            button_reply_id,
            stored_platform=stored_platform,
        )

    unresolved_signal = bool(turn_meta.get("unresolved_signal_current_turn"))
    unresolved_rounds = int(turn_meta.get("unresolved_rounds") or 0)
    unresolved_phase = (turn_meta.get("unresolved_phase") or "none").strip().lower()
    use_platform_only_retrieval = bool(turn_meta.get("use_platform_only_retrieval"))
    runtime_context["use_platform_only_retrieval"] = use_platform_only_retrieval
    runtime_context["unresolved_phase"] = unresolved_phase
    if use_platform_only_retrieval:
        runtime_context["known_os_version"] = ""
        runtime_context["known_model"] = ""

    refinement_platform = (
        runtime_context.get("retrieval_platform")
        or runtime_context.get("stored_platform")
        or runtime_context.get("known_platform")
        or refinement_platform_for_turn(db, thread_id, history)
        or ""
    ).strip().lower()
    runtime_context["unresolved_rounds"] = unresolved_rounds
    runtime_context["unresolved_signal_current_turn"] = unresolved_signal
    if (
        on_tech_path
        and not scam_mode
        and refinement_platform
        and not use_platform_only_retrieval
    ):
        refinement_check = check_platform_refinement(current_message, refinement_platform)
        if refinement_check.get("status") == "unsupported":
            return _unsupported_refinement_offer_reply(
                customer_name=customer_name,
                platform=refinement_platform,
                unsupported_label=refinement_check.get("label") or "",
                refinement_type=refinement_check.get("refinement_type") or "",
            )

    if unresolved_phase == UNRESOLVED_PHASE_DIAGNOSTIC:
        prior_steps = _last_assistant_troubleshooting_from_history(history)
        return _unresolved_diagnostic_reply(
            customer_name=customer_name,
            pending_user_issue=pending_user_issue,
            platform=(
                runtime_context.get("retrieval_platform")
                or runtime_context.get("stored_platform")
                or runtime_context.get("known_platform")
                or ""
            ),
            prior_steps=prior_steps,
        )

    platform_issue_slug = _should_ask_tech_issue_before_retrieval(
        on_tech_path=on_tech_path,
        scam_mode=scam_mode,
        pending_user_issue=pending_user_issue,
        unresolved_signal=unresolved_signal,
        current_message=current_message,
        button_reply_id=button_reply_id,
    )
    if platform_issue_slug:
        return {
            "kind": "text",
            "message": _issue_prompt_for_platform(platform_issue_slug),
            "message_source": "platform_issue_prompt",
        }

    if (
        scam_mode
        and not on_tech_path
        and not _user_has_described_scam_situation(
            history, current_message, button_reply_id
        )
    ):
        if not known_phone_os and _is_scam_entry_message(current_message, button_reply_id):
            os_prompt = dynamic_copy(
                "scam_os_prompt",
                context={"customer_name": (customer_name or "").strip()},
            )
            return {
                "kind": "action",
                "action": "scam_os_buttons",
                "message": os_prompt,
                "message_source": "scam_os_prompt,dynamic_copy",
            }
        if known_phone_os and (
            _is_scam_os_reply(button_reply_id)
            or _is_scam_entry_message(current_message, button_reply_id)
        ):
            entry_text = dynamic_copy(
                "scam_entry",
                context={"customer_name": (customer_name or "").strip()},
            )
            return {
                "kind": "text",
                "message": entry_text,
                "message_source": "scam_entry,dynamic_copy",
            }

    messages = _to_langchain_messages(history)
    messages.insert(
        1,
        SystemMessage(
            content=(
                "<runtime_context>\n"
                f"fresh_chatbot_thread: {fresh_chatbot_thread}\n"
                f"post_resolve_welcome_sent: {str(post_resolve_welcome_sent).lower()}\n"
                f"prior_welcome_blend: {prior_welcome_blend or 'none'}\n"
                f"prior_issue_topic: {prior_issue_topic or 'none'}\n"
                f"prior_thread_summary: {prior_thread_summary or 'none'}\n"
                f"prior_turns_snippet: {prior_turns_snippet or 'none'}\n"
                f"support_mode: {support_mode or 'none'}\n"
                f"known_platform: {runtime_context.get('known_platform') or 'unknown'}\n"
                f"stored_platform: {runtime_context.get('stored_platform') or 'none'}\n"
                f"needs_platform_pick: {str(runtime_context.get('needs_platform_pick')).lower()}\n"
                f"retrieval_platform: {runtime_context.get('retrieval_platform') or 'none'}\n"
                f"known_phone_os: {known_phone_os or 'unknown'}\n"
                f"known_os_version: {runtime_context.get('known_os_version') or 'unknown'}\n"
                f"known_model: {runtime_context.get('known_model') or 'unknown'}\n"
                f"use_platform_only_retrieval: {str(use_platform_only_retrieval).lower()}\n"
                f"customer_name: {customer_name or 'unknown'}\n"
                f"latest_user_message: {(current_message or '').strip() or 'none'}\n"
                f"recent_user_messages:\n{recent_user_messages}\n"
                f"button_reply_id: {runtime_context.get('button_reply_id') or 'none'}\n"
                f"unresolved_signal_current_turn: {runtime_context.get('unresolved_signal_current_turn')}\n"
                f"unresolved_rounds: {runtime_context.get('unresolved_rounds')}\n"
                f"unresolved_phase: {unresolved_phase or 'none'}\n"
                f"bank_helpline_url: {bank_helpline_url or 'none'}\n"
                f"scam_mode_active: {str(scam_mode).lower()}\n"
                f"active_branch: {(turn_meta.get('active_branch') or support_mode or 'infer_from_history')}\n"
                f"pending_user_issue: {pending_user_issue or 'none'}\n"
                "Before RULE 3: read recent_user_messages and full history — not only latest_user_message.\n"
                "If the user already described a tech goal or problem in an earlier message and the latest message only selects Tech Help or a phone brand, issue is known: use RULE 2b (search_support_docs); do not ask what issue they are having.\n"
                "Routing (tech vs scam) is YOUR job via <routing_intelligence> and RULE 0 — read full history every turn.\n"
                "support_mode / scam_mode_active / active_branch are hints only — if history clearly shows scam safety, follow RULE 0 even when flags were false on a typed-first message.\n"
                "Never close a scam crisis with a generic 'glad everything is working' line; never send platform_buttons mid-scam.\n"
                "Mid-conversation branch change (tech ↔ scam): plain text only — ask warmly if they want scam safety or phone help; yes/no or their next message confirms the branch.\n"
                "After user confirms switching branch: help immediately using pending_user_issue and history — do not re-ask the same question.\n"
                "Infer from history whether the user confirmed money sent or OTP/password/bank details shared; only then use URGENT/SECTION E bank-block steps.\n"
                "FIRST URGENT after OTP/credentials: Step 1 = call bank to block only — NO bank phone number unless user already named that bank in history; never default to HDFC. Closing must offer helpline when they reply with bank name.\n"
                "Bank helpline follow-up: when user names a bank or asks for number → one line from scam_reference_numbers only; no 1930/complaint in same turn.\n"
                "When on scam path: plain text only; no send_mode_buttons, send_platform_buttons, send_feedback_buttons, or search_support_docs.\n"
                "Scam Help entry (no story yet): system asks phone OS once (iOS/Android buttons) only on scam branch — NEVER on tech branch.\n"
                "When support_mode or active_branch is tech: never send or reference scam OS buttons; known_phone_os must stay unknown.\n"
                "When known_phone_os is ios: never suggest APK sideloading or Android-only app install steps; iPhones use App Store only — for fake apps use delete app / Settings > General > iPhone Storage, or remove unknown configuration profiles.\n"
                "When known_phone_os is android: APK/fake-app guidance from scam flow applies (do not open, uninstall via Settings > Apps).\n"
                "Do not re-ask iOS vs Android once known_phone_os is set.\n"
                "When stored_platform is set (device saved in DB), infer known_phone_os: apple→ios, samsung/pixel/oppo/xiaomi→android; do not show iOS/Android buttons.\n"
                "search_support_docs MUST use retrieval_platform from runtime context (from device_id in DB); never override with chat-inferred brand.\n"
                "Do not call send_platform_buttons when stored_platform is set unless needs_platform_pick is true or user switched phone.\n"
                "When the user has described their situation: MUST call search_scam_kb first, then <scam_unified_flow> (comfort → category → MO; contextual check — not always money/OTP).\n"
                "Complaint (1930): plain text REPORT/HOW TO FILE only; end with one warm natural line offering prevention tips (no 'reply yes'). If user clearly wants tips: search_scam_kb(prevention) then PREVENTION in plain text.\n"
                "Never repeat prior-thread welcome-back text when entering scam help or when the user already described a scam in this thread.\n"
                "When on scam path and user clearly thanks or says resolved: conversation_control(resolved) + warm close — no mode buttons.\n"
                "When on scam path and user asks for human: RULE 7b — brief acknowledgment only.\n"
                "When fresh_chatbot_thread is true and post_resolve_welcome_sent is true: system already sent prior_welcome_blend + Tech/Scam buttons — do NOT send_mode_buttons or repeat welcome on greeting-only.\n"
                "When fresh_chatbot_thread is true and post_resolve_welcome_sent is false and greeting-only: prior_welcome_blend may be used in send_mode_buttons (human handoff return).\n"
                "When unresolved_phase is diagnostic, the system owns that turn — you are not invoked.\n"
                "When unresolved_phase is refined_retry, search_support_docs is pre-loaded — call send_feedback_buttons only, using the same STEP STYLE as first-pass fixes (RULE 1/6); include one short line on what is different; do not ask diagnostic questions or call search_support_docs again.\n"
                "When use_platform_only_retrieval is true: call search_support_docs with retrieval_platform only — omit os_version and device model.\n"
                "When user gave an unsupported OS/model/version, system asks Yes/No for general platform-only steps — do not retrieve until they confirm Yes.\n"
                "</runtime_context>"
            )
        ),
    )
    retrieval_sources: list[str] = []
    retrieval_confidence = 0.0
    had_search_support_this_turn = False
    had_search_scam_this_turn = False
    on_scam_path = scam_mode
    refined_retry_preloaded = False

    if unresolved_phase == UNRESOLVED_PHASE_REFINED_RETRY:
        retrieval = retrieve_for_refined_unresolved_turn(
            history=history,
            current_message=current_message,
            pending_user_issue=pending_user_issue,
            platform=refinement_platform,
            use_platform_only_retrieval=use_platform_only_retrieval,
        )
        if retrieval.get("kind"):
            return retrieval
        tool_call_id = "refined-retry-search"
        messages.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_support_docs",
                        "args": {
                            "user_query": (pending_user_issue or current_message or "").strip()
                        },
                        "id": tool_call_id,
                    }
                ],
            )
        )
        messages.append(
            ToolMessage(
                content=(retrieval.get("context") or ""),
                tool_call_id=tool_call_id,
                name="search_support_docs",
            )
        )
        messages.append(
            HumanMessage(
                content=(
                    "The user replied after a Still Stuck diagnostic with refinement details. "
                    "search_support_docs results are above. Call send_feedback_buttons NOW with "
                    "updated troubleshooting steps using the same STEP STYLE as first-pass fixes "
                    "(numbered conversational steps, optional intro with customer_name, check-in per RULE 6). "
                    "Include one short line on what is different from prior steps. "
                    "Do NOT call search_support_docs again. Do NOT ask diagnostic questions."
                )
            )
        )
        retrieval_sources = list(retrieval.get("sources") or [])
        confidence = retrieval.get("confidence_score")
        if isinstance(confidence, (int, float)):
            retrieval_confidence = float(confidence)
        had_search_support_this_turn = True
        refined_retry_preloaded = True

    for _ in range(MAX_TOOL_STEPS):
        on_scam_path = _effective_scam_path(
            scam_mode,
            had_search_scam=had_search_scam_this_turn,
            button_reply_id=button_reply_id,
        )
        ai_message = MAIN_LLM.invoke(messages)
        tool_calls = getattr(ai_message, "tool_calls", None) or []

        if not tool_calls:
            text = (getattr(ai_message, "content", "") or "").strip()
            if not text:
                fallback = _fallback_reply_text(on_scam_path)
                if on_scam_path:
                    return _scam_text_reply(
                        fallback,
                        retrieval_sources if had_search_scam_this_turn else None,
                    )
                return {
                    "kind": "text",
                    "message": fallback,
                    "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                }
            if (
                not on_scam_path
                and had_search_support_this_turn
                and text
                and _looks_like_troubleshooting_steps(text)
            ):
                return {
                    "kind": "action",
                    "action": "feedback_buttons",
                    "message": text,
                    "message_source": ",".join(
                        (["refined_unresolved"] if refined_retry_preloaded else [])
                        + retrieval_sources
                    )
                    if (refined_retry_preloaded or retrieval_sources)
                    else "",
                    "confidence_score": retrieval_confidence,
                }
            if on_scam_path:
                return _scam_text_reply(text, retrieval_sources if had_search_scam_this_turn else None)
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
                platform = (runtime_context.get("retrieval_platform") or "").strip().lower()
                if refined_retry_preloaded:
                    messages.append(
                        ToolMessage(
                            content=(
                                "Search already completed for this refined Still Stuck retry. "
                                "Call send_feedback_buttons with updated steps."
                            ),
                            tool_call_id=tool_call_id,
                            name="search_support_docs",
                        )
                    )
                    continue
                if on_scam_path:
                    messages.append(
                        ToolMessage(
                            content=(
                                "User is on the scam safety path. Do not call search_support_docs. "
                                "Reply in plain text following RULE 0 and <scam_unified_flow>."
                            ),
                            tool_call_id=tool_call_id,
                            name="search_support_docs",
                        )
                    )
                    continue
                if not platform:
                    return {
                        "kind": "action",
                        "action": "platform_buttons",
                        "message": "Which phone are you using?",
                    }
                base_query = str(args.get("user_query") or current_message or "").strip()
                blocked_slug = _should_ask_tech_issue_before_retrieval(
                    on_tech_path=on_tech_path,
                    scam_mode=scam_mode,
                    pending_user_issue=pending_user_issue,
                    unresolved_signal=unresolved_signal,
                    current_message=current_message,
                    button_reply_id=button_reply_id,
                    user_query=base_query,
                )
                if blocked_slug:
                    return {
                        "kind": "text",
                        "message": _issue_prompt_for_platform(blocked_slug),
                        "message_source": "platform_issue_prompt",
                    }
                args["platform"] = platform
                known_os_version = (runtime_context.get("known_os_version") or "").strip().lower()
                known_model = (runtime_context.get("known_model") or "").strip().lower()
                if use_platform_only_retrieval:
                    known_os_version = ""
                    known_model = ""
                elif platform:
                    refinement_check = check_platform_refinement(
                        current_message, platform
                    )
                    if refinement_check.get("status") == "unsupported":
                        return _unsupported_refinement_offer_reply(
                            customer_name=customer_name,
                            platform=platform,
                            unsupported_label=refinement_check.get("label") or "",
                            refinement_type=refinement_check.get("refinement_type") or "",
                        )
                retrieval_os, retrieval_version = _map_retrieval_os_and_version(
                    platform, known_os_version, known_model
                )
                if not str(args.get("os_version") or "").strip() and retrieval_version:
                    args["os_version"] = retrieval_version
                if (
                    (
                        _is_branch_change_short_reply(current_message)
                        or _user_switch_reply(current_message)
                    )
                    and pending_user_issue
                    and (not base_query or len(base_query) < 12)
                ):
                    base_query = pending_user_issue
                hint_lines = []
                if retrieval_os:
                    hint_lines.append(f"os_family: {retrieval_os}")
                if retrieval_version:
                    hint_lines.append(f"os_version: {retrieval_version}")
                if known_model and platform == "samsung":
                    hint_lines.append(f"device_model: {known_model}")
                if hint_lines:
                    args["user_query"] = f"{base_query}\n" + "\n".join(hint_lines)
                elif base_query != str(args.get("user_query") or "").strip():
                    args["user_query"] = base_query
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

            if tool_name == "search_scam_kb":
                if not _user_has_described_scam_situation(history, current_message, button_reply_id):
                    messages.append(
                        ToolMessage(
                            content=(
                                "User has not described their situation yet. Reassure warmly and ask "
                                "what happened (one short question). Do not retrieve until they explain."
                            ),
                            tool_call_id=tool_call_id,
                            name="search_scam_kb",
                        )
                    )
                    continue
                query = str(args.get("user_query") or current_message or "").strip()
                if not query or len(query) < 15:
                    parts = []
                    for item in history:
                        if item.get("role") == "user":
                            c = (item.get("content") or "").strip()
                            if c and not _is_scam_entry_message(c):
                                parts.append(c)
                    if (current_message or "").strip() and not _is_scam_entry_message(
                        current_message, button_reply_id
                    ):
                        parts.append((current_message or "").strip())
                    query = " ".join(parts[-4:]) or query
                try:
                    result = search_scam_kb.invoke({"user_query": query})
                except Exception:
                    logger.exception("search_scam_kb invocation failed")
                    messages.append(
                        ToolMessage(
                            content=_format_scam_kb_tool_message(""),
                            tool_call_id=tool_call_id,
                            name="search_scam_kb",
                        )
                    )
                    continue
                messages.append(
                    ToolMessage(
                        content=_format_scam_kb_tool_message(result.get("context", "") or ""),
                        tool_call_id=tool_call_id,
                        name="search_scam_kb",
                    )
                )
                sources = result.get("sources") or []
                if isinstance(sources, list):
                    retrieval_sources = [str(item) for item in sources if str(item).strip()]
                confidence = result.get("confidence_score")
                if isinstance(confidence, (int, float)):
                    retrieval_confidence = float(confidence)
                search_tool_used = True
                had_search_scam_this_turn = True
                on_scam_path = True
                continue

            if tool_name == "send_mode_buttons":
                if on_scam_path:
                    return _scam_text_reply(
                        args.get("message") or "",
                        retrieval_sources if had_search_scam_this_turn else None,
                    )
                return {
                    "kind": "action",
                    "action": "mode_buttons",
                    "message": (args.get("message") or "").strip(),
                    "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                }

            if tool_name == "send_platform_buttons":
                if on_scam_path:
                    return _scam_text_reply(
                        args.get("message") or "",
                        retrieval_sources if had_search_scam_this_turn else None,
                    )
                msg = (args.get("message") or "").strip()
                needs_platform_pick = bool(runtime_context.get("needs_platform_pick"))
                stored_platform = (runtime_context.get("stored_platform") or "").strip().lower()
                switching = _user_switching_platform(
                    stored_platform, current_message, button_reply_id
                )
                if stored_platform in SUPPORTED_PLATFORMS and not needs_platform_pick and not switching:
                    return {
                        "kind": "text",
                        "message": msg,
                        "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                    }
                if not needs_platform_pick and not switching:
                    known_platform = (runtime_context.get("known_platform") or "").strip().lower()
                    if known_platform in SUPPORTED_PLATFORMS:
                        return {
                            "kind": "text",
                            "message": msg,
                            "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                        }
                return {
                    "kind": "action",
                    "action": "platform_buttons",
                    "message": msg,
                    "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                }

            if tool_name == "send_feedback_buttons":
                if on_scam_path:
                    return _scam_text_reply(
                        args.get("message") or "",
                        retrieval_sources if had_search_scam_this_turn else None,
                    )
                msg = (args.get("message") or "").strip()
                if not _looks_like_troubleshooting_steps(msg):
                    return {
                        "kind": "text",
                        "message": msg,
                        "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
                    }
                result = {
                    "kind": "action",
                    "action": "feedback_buttons",
                    "message": msg,
                    "message_source": ",".join(
                        (["refined_unresolved"] if refined_retry_preloaded else [])
                        + retrieval_sources
                    )
                    if (refined_retry_preloaded or retrieval_sources)
                    else "",
                }
                if had_search_support_this_turn:
                    result["confidence_score"] = retrieval_confidence
                return result

            if tool_name == "conversation_control":
                return {"kind": "control", "action": (args.get("action") or "").strip()}

        if search_tool_used:
            continue

        text = (getattr(ai_message, "content", "") or "").strip()
        if (
            not on_scam_path
            and had_search_support_this_turn
            and text
            and _looks_like_troubleshooting_steps(text)
        ):
            return {
                "kind": "action",
                "action": "feedback_buttons",
                "message": text,
                "message_source": ",".join(
                    (["refined_unresolved"] if refined_retry_preloaded else [])
                    + retrieval_sources
                )
                if (refined_retry_preloaded or retrieval_sources)
                else "",
                "confidence_score": retrieval_confidence,
            }
        if on_scam_path and text:
            return _scam_text_reply(
                text,
                retrieval_sources if had_search_scam_this_turn else None,
            )
        if not text:
            text = _fallback_reply_text(on_scam_path)
        return {
            "kind": "text",
            "message": text,
            "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
        }

    fallback = _fallback_reply_text(on_scam_path)
    if on_scam_path:
        return _scam_text_reply(
            fallback,
            retrieval_sources if had_search_scam_this_turn else None,
        )
    return {
        "kind": "text",
        "message": fallback,
        "message_source": ",".join(retrieval_sources) if retrieval_sources else "",
    }
