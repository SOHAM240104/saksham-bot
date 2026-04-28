import streamlit as st
from app.llm.rag_chain import call_support_docs_tool, stream_rag_response
import time


# -------------------------
# TIMING HELPER
# -------------------------
def log_time(label, start_time):
    elapsed = time.time() - start_time
    print(f"[TIMING] {label}: {elapsed:.3f}s")


# -------------------------
# DEVICE DETECTION
# -------------------------
def detect_device(text):
    text = text.lower()

    if any(x in text for x in ["iphone", "ipad", "apple", "ios"]):
        return "apple"
    if "samsung" in text:
        return "samsung"
    if "pixel" in text or "google" in text:
        return "pixel"
    if "oppo" in text:
        return "oppo"
    if any(x in text for x in ["mi", "redmi", "xiaomi", "poco"]):
        return "xiaomi"

    return None


PLATFORM_OPTIONS = ["Apple", "Samsung", "Xiaomi", "Oppo", "Pixel"]

WORKED_YES = "Yes"
WORKED_NO = "No"
ANYTHING_ELSE_YES = "Yes, I need more help"
ANYTHING_ELSE_NO = "No, thanks"

MIN_TOP_DOC_CONFIDENCE = 0.2

SUPPORT_INTENT_KEYWORDS = {
    "customer care",
    "support number",
    "helpline",
    "contact",
    "service center",
    "website",
    "toll free",
    "tollfree",
}

BRAND_SUPPORT_DIRECTORY = {
    "ai+": {"brand": "Ai+", "website": "aiplusstore.com / aiplussmartphone.com", "support": "1800 268 1992"},
    "apple": {"brand": "Apple", "website": "www.apple.com/in", "support": "1800 1127"},
    "gfive": {"brand": "GFive", "website": "gfive.in", "support": "1800-102-025 / 0120-4060555"},
    "gionee": {"brand": "GIONEE", "website": "gionee.co.in", "support": "1860 258 3138 / 1800 208 1166"},
    "google": {"brand": "Google (Pixel)", "website": "store.google.com/in", "support": "Online/Chat only"},
    "pixel": {"brand": "Google (Pixel)", "website": "store.google.com/in", "support": "Online/Chat only"},
    "hmd": {"brand": "HMD", "website": "www.hmd.com/en_in", "support": "1800 1028 169"},
    "hotline": {"brand": "HOTLINE", "website": "hotlinemobile.in", "support": "Check website"},
    "htc": {"brand": "HTC", "website": "www.htc.com/in", "support": "vive_cares@htc.com"},
    "ikall": {"brand": "I Kall", "website": "www.ikall.in", "support": "1800 120 4934 / 0120-473 1048"},
    "i kall": {"brand": "I Kall", "website": "www.ikall.in", "support": "1800 120 4934 / 0120-473 1048"},
    "infinix": {"brand": "Infinix", "website": "www.infinixmobility.com/in", "support": "1800 419 0525"},
    "iqoo": {"brand": "iQOO", "website": "www.iqoo.com/in", "support": "0120-6283450"},
    "itel": {"brand": "itel", "website": "www.itel-india.com", "support": "1800 4190 525 / 0120-6650 888"},
    "jio": {"brand": "Jio", "website": "www.jio.com", "support": "199 / 1800 88 99999"},
    "jmax": {"brand": "Jmax", "website": "jmaxmobile.com", "support": "079809 19516"},
    "karbonn": {"brand": "Karbonn", "website": "karbonn.in", "support": "1860 500 1492"},
    "kechaoda": {"brand": "Kechaoda", "website": "thekechaoda.com / kechaoda.in", "support": "+91-98555-78560"},
    "lg": {"brand": "LG", "website": "www.lg.com/in", "support": "1800 315 9999 / 1800 180 9999"},
    "micromax": {"brand": "Micromax", "website": "www.micromaxinfo.com", "support": "1800 2020 091 / 0129-4883 999"},
    "mixx": {"brand": "MIXX", "website": "mixxtechnologies.in", "support": "Check website"},
    "motorola": {"brand": "Motorola", "website": "www.motorola.in", "support": "1800 419 6686"},
    "nokia": {"brand": "Nokia", "website": "www.nokia.com/phones/en_in", "support": "1800 1028 169"},
    "nothing": {"brand": "Nothing", "website": "in.nothing.tech", "support": "Online support only"},
    "oneplus": {"brand": "OnePlus", "website": "www.oneplus.in", "support": "WhatsApp 77958 16968"},
    "oppo": {"brand": "OPPO", "website": "www.oppo.com/in", "support": "1800 103 2777"},
    "poco": {"brand": "POCO", "website": "www.poco.in", "support": "1800 572 7626"},
    "realme": {"brand": "realme", "website": "www.realme.com/in", "support": "1800 102 2777"},
    "redmi": {"brand": "REDMI (Xiaomi)", "website": "www.mi.com/in", "support": "1800 103 6286"},
    "xiaomi": {"brand": "REDMI (Xiaomi)", "website": "www.mi.com/in", "support": "1800 103 6286"},
    "mi": {"brand": "REDMI (Xiaomi)", "website": "www.mi.com/in", "support": "1800 103 6286"},
    "samsung": {"brand": "Samsung", "website": "www.samsung.com/in", "support": "1800 40 726 7864 / 1800 5 726 7864"},
    "snexian": {"brand": "Snexian", "website": "thesnexian.com / snexian.com", "support": "Check website"},
    "tecno": {"brand": "Tecno", "website": "www.tecno-mobile.in", "support": "1800 4190 525 / 0120-6650 888"},
    "vivo": {"brand": "Vivo", "website": "www.vivo.com/in", "support": "0120-628 3388"},
}


# -------------------------
# HELPERS
# -------------------------
def _platform_key(label: str):
    return label.lower().strip()


def _is_yes(text: str) -> bool:
    return text.lower().strip() in {"yes", "y", "yes, i need more help"}


def _is_no(text: str) -> bool:
    return text.lower().strip() in {"no", "n", "no, thanks"}


def _has_confident_docs(docs) -> bool:
    if not docs:
        return False
    return float((docs[0].metadata or {}).get("confidence", 0.0)) >= MIN_TOP_DOC_CONFIDENCE


def _build_sources(docs):
    sources = []
    for doc in docs or []:
        meta = doc.metadata or {}

        link = (
            meta.get("source")
            or meta.get("url")
            or meta.get("link")
            or meta.get("source_url")
            or meta.get("file_path")
            or meta.get("path")
        )

        sources.append({
            "confidence": round(float(meta.get("confidence", 0.0)), 3),
            "content": doc.page_content.strip(),
            "link": link,
            "metadata": meta,
        })

    return sources


def _is_brand_support_query(text: str) -> bool:
    text_l = text.lower()
    return any(keyword in text_l for keyword in SUPPORT_INTENT_KEYWORDS)


def _match_brand_support(text: str):
    text_l = text.lower()
    for alias, info in BRAND_SUPPORT_DIRECTORY.items():
        if alias in text_l:
            return info
    return None


def _build_brand_support_response(text: str):
    info = _match_brand_support(text)
    if not info:
        return {
            "text": "I don’t have the support details for that brand. Please check the official website.",
            "buttons": [],
        }

    return {
        "text": (
            f"Brand: {info['brand']}\n"
            f"Website: {info['website']}\n"
            f"Support: {info['support']}"
        ),
        "buttons": [],
    }


# -------------------------
# RETRIEVAL
# -------------------------
def _get_retrieval_(query, os_version=None):
    t = time.time()

    profile = st.session_state.profile

    docs, context, tool_call = call_support_docs_tool(
        user_query=query,
        platform=profile.get("platform"),
        os_version=os_version,
    )

    log_time("RETRIEVAL", t)
    return docs, context, tool_call


# -------------------------
# SOLUTION
# -------------------------
def _retrieval_caller(query, os_version=None):
    profile = st.session_state.profile

    docs, context, tool_call = _get_retrieval_(query, os_version=os_version)

    if not _has_confident_docs(docs):
        return {
            "text": "I can help only with smartphone technical issues.",
            "buttons": [],
        }

    return {
        "stream_generator": stream_rag_response(
            user_query=query,
            context=context,
            device=profile.get("platform"),
            name=profile.get("name") or "User",
            os_version=os_version,
        ),
        "stream_intro": "Let us try this together:\n\n",
        "stream_outro": "\n\nDid this solve your problem?",
        "buttons": [WORKED_YES, WORKED_NO],
        "sources": _build_sources(docs),
    }


# -------------------------
# MAIN CHAT
# -------------------------
def handle_chat(user_input):
    profile = st.session_state.profile
    state = st.session_state.flow_state
    user_text = user_input.strip()

    # init flags
    profile.setdefault("problem", None)
    profile.setdefault("os_version", None)
    profile.setdefault("retry_done", False)
    profile.setdefault("awaiting_solution_feedback", False)
    profile.setdefault("awaiting_os_version", False)
    profile.setdefault("awaiting_anything_else", False)

    # -------------------------
    # NAME
    # -------------------------
    if state == "ASK_NAME":
        profile["name"] = user_text
        st.session_state.flow_state = "ASK_HELP_TYPE"

        return {
            "text": f"Nice to have you! Before i help, can i know what mode you want to explore?",
            "buttons": ["Tech Help", "Scam Check"],
        }

    # -------------------------
    # MODE
    # -------------------------
    if state == "ASK_HELP_TYPE":
        if user_text == "Scam Check":
            return {
                "text": "We are working on scam detection.",
                "buttons": ["Tech Help"],
            }

        if user_text.lower() in {"tech help", "tech"}:
            st.session_state.flow_state = "TECH_CHAT"
            return {"text": "Which phone are you using?", "buttons": PLATFORM_OPTIONS}

        return {"text": "How can I help?", "buttons": ["Tech Help", "Scam Check"]}

    # -------------------------
    # TECH CHAT
    # -------------------------
    if state == "TECH_CHAT":
        # Brand support directory lookup should work regardless of platform state.
        if _is_brand_support_query(user_text):
            return _build_brand_support_response(user_text)

        # ANYTHING ELSE FLOW
        if profile.get("awaiting_anything_else"):
            if _is_yes(user_text):
                profile["problem"] = None
                profile["awaiting_anything_else"] = False
                return {"text": "What issue are you facing?", "buttons": []}

            if _is_no(user_text):
                # RESET FLOW (keep name)
                profile["problem"] = None
                profile["os_version"] = None
                profile["retry_done"] = False
                profile["awaiting_solution_feedback"] = False
                profile["awaiting_os_version"] = False
                profile["awaiting_anything_else"] = False
                profile["platform"] = None

                st.session_state.flow_state = "ASK_HELP_TYPE"

                return {
                    "text": "What would you like to do next?",
                    "buttons": ["Tech Help", "Scam Check"],
                }

            return {
                "text": "Do you need help with anything else?",
                "buttons": [ANYTHING_ELSE_YES, ANYTHING_ELSE_NO],
            }

        # PLATFORM
        if not profile.get("platform"):
            platform = _platform_key(user_text) or detect_device(user_text)
            if platform:
                profile["platform"] = platform
                return {"text": "What issue are you facing?", "buttons": []}
            return {"text": "Which phone are you using?", "buttons": PLATFORM_OPTIONS}

        # OS VERSION
        if profile.get("awaiting_os_version"):
            profile["os_version"] = user_text
            profile["awaiting_os_version"] = False
            profile["awaiting_solution_feedback"] = True
            profile["retry_done"] = True

            return _retrieval_caller(profile["problem"], user_text)

        # FEEDBACK
        if profile.get("awaiting_solution_feedback"):
            if _is_yes(user_text):
                profile["awaiting_solution_feedback"] = False
                profile["awaiting_anything_else"] = True
                return {
                    "text": "Do you need help with anything else?",
                    "buttons": [ANYTHING_ELSE_YES, ANYTHING_ELSE_NO],
                }

            if _is_no(user_text):
                profile["awaiting_solution_feedback"] = False

                if profile.get("retry_done"):
                    # RESET FLOW
                    profile["problem"] = None
                    profile["os_version"] = None
                    profile["retry_done"] = False
                    profile["platform"] = None

                    st.session_state.flow_state = "ASK_HELP_TYPE"

                    return {
                        "text": "Support agent will connect.\n\nWhat would you like to do next?",
                        "buttons": ["Tech Help", "Scam Check"],
                    }

                profile["awaiting_os_version"] = True
                return {"text": "What is your OS version?", "buttons": []}

            return {
                "text": "Did this solve your problem?",
                "buttons": [WORKED_YES, WORKED_NO],
            }

        # NEW PROBLEM
        profile["problem"] = user_text
        profile["awaiting_solution_feedback"] = True

        return _retrieval_caller(user_text)

    # -------------------------
    # FALLBACK
    # -------------------------
    st.session_state.flow_state = "ASK_NAME"
    return {"text": "Let's start again.", "buttons": []}