"""Shared LLM-backed outbound copy for webhook handling and conversation."""

import json
import logging
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

logger = logging.getLogger("wati.services.dynamic_copy")
COPY_LLM = ChatOpenAI(model="gpt-4.1-mini", temperature=0.6)


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
    return fallback.get(kind, "How can I help you?")
