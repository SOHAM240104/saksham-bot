"""Shared LLM-backed outbound copy for webhook handling and conversation."""

import json
import logging
import re
import time
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

logger = logging.getLogger("app.services.chat.copy")

# Fast copy model: short UX sentences only (not troubleshooting agent).
COPY_LLM = ChatOpenAI(model="gpt-4o-mini", temperature=0.6, max_tokens=120)


def _load_dynamic_copy_prompt() -> str:
    fallback = (
        "You are generating outbound Saksham website chat copy.\n"
        "Return exactly ONE sentence, plain text only.\n"
        "No emojis. No bullets. No markdown.\n"
        "Tone: warm, calm, human.\n"
        "Keep it concise."
    )
    try:
        path = Path(__file__).resolve().parent / "systemprompt.txt"
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


def _display_copy_name(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s:
        return ""
    if sum(1 for c in s if c.isalpha()) < 2:
        return s
    return " ".join(
        (w[0].upper() + w[1:].lower()) if w[:1].isalpha() else w for w in s.split()
    )


def _fallback_text(kind: str, context: dict) -> str:
    name = _display_copy_name(context.get("customer_name") or "")
    ji = f"{name} Ji, " if name else ""
    platform_label = (context.get("platform_label") or "your phone").strip()
    issue_topic = (context.get("issue_topic") or "your previous phone issue").strip()
    fallback = {
        "handoff_wait": "I'm connecting you to a human support agent now. Please wait a moment.",
        "handoff_confirm": "Would you like me to connect you to a human agent now? Please reply Yes or No.",
        "resolved_ack": "Happy to know this helped.",
        "resolved_ack_pause": (
            f"{ji}alright — we'll stop here for now."
            if name
            else "Alright — we'll stop here for now."
        ),
        "scam_resolved_ack": (
            "Thank you for trusting us. You did the right thing by checking — stay safe."
        ),
        "resolved_next": "What else can I help you with today?",
        "feedback_checkin": "Please try this once and tell me what you see now.",
        "feedback_ack_hold": (
            f"{ji}take your time with those steps — tell me how it goes when you're ready."
            if name
            else "Take your time with those steps — tell me how it goes when you're ready."
        ),
        "welcome_back_context": (
            f"Welcome back, {name} Ji! How can I help you today?"
            if name
            else "Welcome back! How can I help you today?"
        ),
        "thread_topic": "your previous phone issue",
        "welcome_back_blended": "",  # built below with article polish
        "scam_redirect": (
            "We're still working on the scam-help part. "
            "I can help with phone issues — please choose your phone brand to continue."
        ),
        "scam_entry": (
            f"{ji}you did exactly the right thing by coming to us.\n\n"
            "Can you please tell me what happened — was it a call, message, payment, link, or app?"
            if name
            else (
                "You did exactly the right thing by coming to us.\n\n"
                "Can you please tell me what happened — was it a call, message, payment, link, or app?"
            )
        ),
        "scam_story_ask": (
            f"{ji}in a few words, what happened? Was it a call, message, payment, link, or app?"
            if name
            else "Thanks — in a few words, what happened? Was it a call, message, payment, link, or app?"
        ),
        "scam_os_prompt": (
            "To guide you better, is your phone an iPhone/iPad or Android?"
        ),
        "branch_clarify_bank": (
            "Did you mean your phone issue, or something about your bank or a possible scam?"
        ),
        "platform_still_using": (
            f"Are you still using your {platform_label}? Please reply Yes or No."
        ),
        "platform_issue_ask": (
            f"{ji}what's giving you trouble on your {platform_label} today?"
            if name
            else f"What's giving you trouble on your {platform_label} today?"
        ),
        "unresolved_diagnostic": (
            "Sorry it's still stuck — what happened when you tried the last steps? "
            "If you know your phone software version, that helps — if not, no worry."
        ),
        "unsupported_refinement_offer": (
            "I don't have troubleshooting for that on your phone. "
            "Would you like me to try general steps without that version? Please reply Yes or No."
        ),
        "unsupported_refinement_declined": (
            "No problem — if you share a supported version or model later, I can help with more specific steps."
        ),
    }
    if kind == "welcome_back_blended":
        topic = issue_topic
        if topic.lower() in {"", "your previous phone issue", "none", "n/a"}:
            return fallback["welcome_back_context"]
        if not re.match(r"(?i)^(the|a|an|your|my)\b", topic):
            topic = f"the {topic}"
        if name:
            return (
                f"Welcome back, {name} Ji! Quick check — how's {topic} going? "
                "How can I help you today?"
            )
        return (
            f"Welcome back! Quick check — how's {topic} going? "
            "How can I help you today?"
        )
    if kind == "unresolved_diagnostic":
        hint = (context.get("os_refinement_hint") or "").strip()
        if not hint:
            hint = "phone software version from Settings"
        prefix = f"{name} Ji, " if name else ""
        return (
            f"{prefix}sorry it's still stuck — what happened when you tried the last steps? "
            f"If you know your {hint}, that helps — if not, no worry."
        )
    if kind == "unsupported_refinement_offer":
        label = (context.get("unsupported_label") or "that").strip()
        word = (context.get("refinement_word") or "version").strip()
        prefix = f"{name} Ji, " if name else ""
        return (
            f"{prefix}I don't have troubleshooting guides for {label} on {platform_label}. "
            f"Would you like me to try general steps for your {platform_label} without that {word}? "
            "Please reply Yes or No."
        )
    return fallback.get(kind, "How can I help you?")


def dynamic_copy(kind: str, context: dict | None = None) -> str:
    """Generate warm one-line UX copy via LLM; fall back to curated text on failure."""
    context = context or {}
    goals = {
        "handoff_wait": "Tell user we are connecting to a human support agent and ask them to wait.",
        "handoff_confirm": "Ask user if they want to connect to a human agent now; ask to reply Yes or No.",
        "resolved_ack": (
            "User tapped Resolved or clearly said the phone issue is fixed. "
            "ONE short warm acknowledgment only — do not claim they are done forever."
        ),
        "resolved_ack_pause": (
            "User signed off mid-help (farewell) without saying the issue is fixed. "
            "ONE short line: we stop here for now — do NOT say glad/sorted/resolved/fixed. "
            "Use customer_name as '<Name> Ji' when provided."
        ),
        "scam_resolved_ack": (
            "Warm closing acknowledgment after scam help is finished — thank them for trusting you, "
            "reassure briefly, no steps."
        ),
        "resolved_next": "Ask what else the assistant can help with in a friendly way.",
        "feedback_checkin": "Write a natural one-line follow-up check-in after troubleshooting steps. Avoid 'Did this help?'.",
        "feedback_ack_hold": (
            "User sent a brief soft acknowledgment (ok/okay) after troubleshooting steps. "
            "They are still mid-help — not done. Write ONE short open sentence only, like: "
            "take your time with those steps — tell me how it goes when you're ready. "
            "Do NOT sound like a goodbye, wrap-up, or 'further assistance'. "
            "Do NOT tell them to tap buttons. Do NOT say Resolved or Still Stuck. "
            "Use customer_name as '<Name> Ji' when provided."
        ),
        "welcome_back_context": (
            "Given prior thread history, write one short warm welcome-back line that "
            "references previous help naturally and ends with: How can I help you today? "
            "Use customer_name as '<Name> Ji' when provided. "
            "Do NOT mention human agents, handoff, or connecting to support."
        ),
        "thread_topic": (
            "Given prior thread history, extract only the main issue topic in 3-10 words, "
            "without resolution status or greeting."
        ),
        "welcome_back_blended": (
            "Write ONE short sentence blending: welcome back, hope they are doing okay with the earlier "
            "issue (use issue_topic from context in plain words — no specs, OS versions, or step lists), "
            "and close once with 'How can I help you today?' in a warm, human tone — not a ticket recap. "
            "Use customer_name as '<Name> Ji' when provided. "
            "NEVER mention human agent, Tech Saathi handoff, or connecting to support."
        ),
        "scam_redirect": (
            "User chose Scam Help. Say we are still working on scam support in a warm way. "
            "Offer phone troubleshooting and ask them to choose their phone brand to continue. "
            "One or two short sentences. Do not mention Tech Help or Scam Help buttons."
        ),
        "scam_entry": (
            "User tapped Scam Help with no story yet. Write in clear simple English only "
            " Warm chat opening: reassure they did the right thing "
            "(use customer_name as '<Name> Ji' when provided). Blank line, then ONE question "
            "what happened — call, message, payment, link, or app. 2-4 short lines; at most "
            "one emoji; never blame the victim."
        ),
        "scam_story_ask": (
            "User already got a short welcome or chose phone OS. ONE short warm sentence only: "
            "ask them to briefly say what happened (call, message, payment, link, or app). "
            "Do not repeat a long reassurance. Use customer_name as '<Name> Ji' when provided."
        ),
        "scam_os_prompt": (
            "User chose Scam Help and phone OS is not known yet. One warm sentence asking "
            "whether they use iPhone/iPad or Android so guidance can match their phone. "
            "Plain text only; do not ask what happened yet."
        ),
        "branch_clarify_bank": (
            "User typed a bank name while phone troubleshooting. ONE short calm sentence: "
            "ask if they meant their phone issue or something about the bank/scam. "
            "Do not start scam urgent steps."
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
            "Write ONE warm combined sentence only: (1) start like "
            "'Sorry it's still stuck — what happened when you tried the last steps?'; "
            "(2) ALWAYS append an OS/model ask using os_refinement_hint from context exactly — "
            "e.g. 'If you know your [hint], that helps — if not, no worry.' "
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
    # Product-critical UX copy: curated only (LLM drifts to reminder / corporate tone).
    if kind in {
        "feedback_ack_hold",
        "unresolved_diagnostic",
        "resolved_ack_pause",
        "welcome_back_blended",
        "welcome_back_context",
    }:
        return _fallback_text(kind, context)
    t0 = time.perf_counter()
    try:
        prompt = _load_dynamic_copy_prompt()
        user_payload = {"goal": goals.get(kind, ""), "context": context}
        r = COPY_LLM.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=json.dumps(user_payload, ensure_ascii=False)),
            ]
        )
        txt = (getattr(r, "content", "") or "").strip()
        ms = (time.perf_counter() - t0) * 1000
        logger.info("TURN_TIMING stage=dynamic_copy kind=%s ms=%.0f ok=%s", kind, ms, bool(txt))
        if txt:
            return txt
    except Exception:
        ms = (time.perf_counter() - t0) * 1000
        logger.exception(
            "Dynamic copy generation failed kind=%s ms=%.0f", kind, ms
        )
    return _fallback_text(kind, context)
