"""Shared LLM-backed outbound copy for webhook handling and conversation."""

import json
import logging
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

logger = logging.getLogger("wati.services.dynamic_copy")
COPY_LLM = ChatOpenAI(model="gpt-4.1-mini", temperature=0.6, max_tokens=1000)


def _load_dynamic_copy_prompt() -> str:
    fallback = (
        "You are generating outbound WhatsApp support copy.\n"
        "Return exactly ONE sentence, plain text only.\n"
        "No emojis. No bullets. No markdown.\n"
        "Tone: warm, calm, human.\n"
        "Keep it concise."
    )
    try:
        path = Path(__file__).resolve().parents[1] / "llm" / "systemprompt.txt"
        text = path.read_text(encoding="utf-8")
    except Exception:
        return fallback
    m = re.search(
        r"<dynamic_copy_prompt>\s*(.*?)\s*</dynamic_copy_prompt>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return fallback
    extracted = (m.group(1) or "").strip()
    return extracted or fallback


def dynamic_copy(kind: str, context: dict | None = None) -> str:
    prompt = _load_dynamic_copy_prompt()
    context = context or {}
    goals = {
        "handoff_wait": "Tell user we are connecting to a human support agent and ask them to wait.",
        "handoff_confirm": "Ask user if they want to connect to a human agent now; ask to reply Yes or No.",
        "resolved_ack": "Acknowledge that issue is resolved in a warm way.",
        "resolved_next": "Ask what else the assistant can help with in a friendly way.",
        "feedback_checkin": "Write a natural one-line follow-up check-in after troubleshooting steps. Avoid 'Did this help?'.",
        "welcome_back_context": (
            "Given prior thread history, write one short warm welcome-back line that "
            "references previous help naturally and ends with: How can I help you today?"
        ),
        "thread_topic": (
            "Given prior thread history, extract only the main issue topic in 3-10 words, "
            "without resolution status or greeting."
        ),
        "welcome_back_blended": (
            "Write ONE short sentence blending: welcome back, hope they are doing okay with the earlier "
            "issue (use issue_topic from context in plain words — no specs, OS versions, or step lists), "
            "and close once with 'How can I help you today?' in a warm, human tone — not a ticket recap."
        ),
        "scam_redirect": (
            "User chose Scam Help. Say we are still working on scam support in a warm way. "
            "Offer phone troubleshooting and ask them to choose their phone brand to continue. "
            "One or two short sentences. Do not mention Tech Help or Scam Help buttons."
        ),
        "scam_entry": (
            "User tapped Scam Help with no story yet. Write in clear simple English only "
            " Warm WhatsApp opening: reassure they did the right thing "
            "(use customer_name as '<Name> Ji' when provided). Blank line, then ONE question "
            "what happened — call, message, payment, link, or app. 2-4 short lines; at most "
            "one emoji; never blame the victim."
        ),
        "scam_os_prompt": (
            "User chose Scam Help and phone OS is not known yet. One warm sentence asking "
            "whether they use iPhone/iPad or Android so guidance can match their phone. "
            "Plain text only; do not ask what happened yet."
        ),
        "platform_still_using": (
            "Ask if the user is still using their phone described by platform_label in context; "
            "mention platform_label naturally; end with a clear Yes or No question."
        ),
        "platform_issue_ask": (
            "User confirmed they are still on the same phone (platform_label in context). "
            "Ask one warm open question about what issue or problem they need help with on that phone."
        ),
        "unresolved_diagnostic": (
            "User says previous troubleshooting steps did not work (Still Stuck). "
            "Write ONE warm combined sentence only: (1) ask what step failed or what they saw instead "
            "for issue_summary on platform_label; (2) append optional clause using os_refinement_hint "
            "from context exactly — e.g. 'If you can, please share your [hint]; if not, no problem.' "
            "Use customer_name as '<Name> Ji' when provided. Do NOT provide numbered steps. Do NOT ask Yes or No."
        ),
        "unsupported_refinement_offer": (
            "User gave an OS/model/version not in our guides for platform_label. "
            "Write exactly TWO short sentences in plain text: "
            "(1) say we do not have troubleshooting for unsupported_label on platform_label; "
            "(2) ask if they want general steps for platform_label without that refinement_word "
            "(use refinement_word from context — 'version' or 'model'); end with Please reply Yes or No."
        ),
        "unsupported_refinement_declined": (
            "User declined general platform-only steps after an unsupported OS/model/version. "
            "One warm sentence: no problem, they can share a supported version/model later or describe a new issue."
        ),
    }
    fallback = {
        "handoff_wait": "I'm connecting you to a human support agent now. Please wait a moment.",
        "handoff_confirm": "Would you like me to connect you to a human agent now? Please reply Yes or No.",
        "resolved_ack": "Happy to know this helped.",
        "resolved_next": "What else can I help you with today?",
        "feedback_checkin": "Please try this once and tell me what you see now.",
        "welcome_back_context": "Hi, I am your Tech Saathi from Saksham. How can I help you today?",
        "thread_topic": "your previous phone issue",
        "welcome_back_blended": "Welcome back. Hope your previous issue is okay now - how can I help today?",
        "scam_redirect": (
            "We're still working on the scam-help part. "
            "I can help with phone issues — please choose your phone brand to continue."
        ),
        "scam_entry": (
            "You did exactly the right thing by coming to us.\n\n"
            "Can you please tell me what happened — was it a call, message, payment, link, or app?"
        ),
        "scam_os_prompt": (
            "To guide you better, is your phone an iPhone/iPad or Android?"
        ),
        "platform_still_using": (
            "Are you still using the same phone? Please reply Yes or No."
        ),
        "platform_issue_ask": (
            "What issue are you having with your phone today?"
        ),
        "unresolved_diagnostic": (
            "What step didn't work, or what did you see instead when you tried those steps?"
        ),
        "unsupported_refinement_offer": (
            "I don't have troubleshooting for that on your phone. "
            "Would you like me to try general steps without that version? Please reply Yes or No."
        ),
        "unsupported_refinement_declined": (
            "No problem — if you share a supported version or model later, I can help with more specific steps."
        ),
    }
    try:
        user_payload = {"goal": goals.get(kind, ""), "context": context}
        r = COPY_LLM.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=json.dumps(user_payload, ensure_ascii=False)),
            ]
        )
        txt = (getattr(r, "content", "") or "").strip()
        if txt:
            return txt
    except Exception:
        logger.exception("Dynamic copy generation failed kind=%s", kind)
    if kind == "unresolved_diagnostic":
        hint = (context.get("os_refinement_hint") or "").strip()
        name = (context.get("customer_name") or "").strip()
        prefix = f"{name} ji, " if name else ""
        base = (
            f"{prefix}what step didn't work or what did you see instead when you tried "
            f"to fix {context.get('issue_summary') or 'this'}?"
        )
        if hint:
            return f"{base} If you can, please share your {hint}; if not, no problem."
        return base
    if kind == "unsupported_refinement_offer":
        label = (context.get("unsupported_label") or "that").strip()
        platform_label = (context.get("platform_label") or "your phone").strip()
        word = (context.get("refinement_word") or "version").strip()
        name = (context.get("customer_name") or "").strip()
        prefix = f"{name} ji, " if name else ""
        return (
            f"{prefix}I don't have troubleshooting guides for {label} on {platform_label}. "
            f"Would you like me to try general steps for your {platform_label} without that {word}? "
            "Please reply Yes or No."
        )
    return fallback.get(kind, "How can I help you?")
