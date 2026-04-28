import streamlit as st
from app.llm.problem_handler import handle_chat
import time

SCAM_KEYWORDS = {
    "scam", "fraud", "otp", "phishing", "suspicious", "message", "sms", "link",
}
TECH_MODE_BUTTONS = {"Tech Help"}
SCAM_MODE_BUTTONS = {"Scam Check"}
FOLLOW_UP_PROMPT = "Did this solve your problem?"


def _is_scam_query(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in SCAM_KEYWORDS)


def _contains_follow_up(text: str) -> bool:
    return FOLLOW_UP_PROMPT.lower() in text.lower()


# -------------------------
# INIT STATE
# -------------------------
def init_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []

    if "profile" not in st.session_state:
        st.session_state.profile = {
            "name": None,
            "platform": None,
            "os_version": None,
            "problem": None,
        }

    if "flow_state" not in st.session_state:
        st.session_state.flow_state = "ASK_NAME"

    if "pending_buttons" not in st.session_state:
        st.session_state.pending_buttons = []

    if "mode" not in st.session_state:
        st.session_state.mode = None

    if "onboarding_shown" not in st.session_state:
        st.session_state.onboarding_shown = False


# -------------------------
# PROCESS INPUT
# -------------------------
def process_user_input(user_text: str):
    start = time.time()

    # MODE SWITCH
    if user_text in TECH_MODE_BUTTONS | SCAM_MODE_BUTTONS:
        st.session_state.mode = "tech" if user_text in TECH_MODE_BUTTONS else "scam"

        if st.session_state.mode == "tech":
            result = handle_chat("Tech Help")
        else:
            result = {
                "text": "We are currently working on scam detection support.",
                "buttons": ["Tech Help"],
            }

        st.session_state.messages.append({
            "role": "assistant",
            "content": result.get("text", "")
        })
        st.session_state.pending_buttons = result.get("buttons", [])
        return

    # STORE USER MESSAGE
    st.session_state.messages.append({"role": "user", "content": user_text})

    # ROUTING
    if _is_scam_query(user_text) or st.session_state.mode == "scam":
        result = {
            "text": "We are currently working on scam detection support.",
            "buttons": ["Tech Help"],
        }
    else:
        result = handle_chat(user_text)

    assistant_msg = {"role": "assistant", "content": ""}

    # -------------------------
    # STREAM RESPONSE
    # -------------------------
    if result.get("stream_generator"):
        with st.chat_message("assistant"):
            st.write(result.get("stream_intro", ""))

            stream_start = time.time()
            streamed_text = st.write_stream(result["stream_generator"])
            print(f"[TIMING] STREAM: {time.time() - stream_start:.3f}s")

            stream_outro = result.get("stream_outro", "")
            if stream_outro and _contains_follow_up(streamed_text):
                stream_outro = ""
            st.write(stream_outro)

        assistant_msg["content"] = (
            result.get("stream_intro", "")
            + streamed_text
            + stream_outro
        )

    # -------------------------
    # NORMAL RESPONSE
    # -------------------------
    else:
        assistant_msg["content"] = result.get("text", "")

    # -------------------------
    # SOURCES (optional)
    # -------------------------
    if result.get("sources"):
        assistant_msg["sources"] = result["sources"]

    # SAVE MESSAGE
    st.session_state.messages.append(assistant_msg)

    # BUTTONS
    st.session_state.pending_buttons = result.get("buttons", [])

    print(f"[TIMING] TOTAL: {time.time() - start:.3f}s")


# -------------------------
# UI
# -------------------------
st.title("Tech Saathi")
init_state()


# -------------------------
# WELCOME
# -------------------------
if not st.session_state.onboarding_shown:
    st.session_state.messages.append({
        "role": "assistant",
        "content": "Namaste\n\nI am your Tech Saathi\n\nWhat should I call you?"
    })
    st.session_state.onboarding_shown = True


# -------------------------
# DISPLAY CHAT
# -------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.write(msg["content"])

        if msg.get("sources"):
            with st.expander("Sources"):
                for i, src in enumerate(msg["sources"], start=1):
                    meta = src.get("metadata", {}) or {}

                    st.markdown(f"**Source {i}**")
                    st.caption(f"Confidence: {src.get('confidence', 0)}")

                    title = meta.get("title")
                    header = meta.get("header")
                    os_name = meta.get("os")
                    link = src.get("link")

                    if title:
                        st.markdown(f"**Title:** {title}")
                    if header:
                        st.markdown(f"**Section:** {header}")
                    if os_name:
                        st.markdown(f"**OS:** {os_name}")

                    if link:
                        st.markdown(f"[Open source link]({link})")

                    snippet = src.get("content", "")
                    if snippet:
                        st.markdown("**Snippet:**")
                        st.markdown(snippet)

                    if meta:
                        with st.expander("View full metadata", expanded=False):
                            st.json(meta)

                    st.divider()

# -------------------------
# MODE SELECT
# -------------------------
if st.session_state.mode is None and st.session_state.flow_state != "ASK_NAME":
    st.write("How can I help you?")

    c1, c2 = st.columns(2)

    if c1.button("Tech Help"):
        process_user_input("Tech Help")
        st.rerun()

    if c2.button("Scam Check"):
        process_user_input("Scam Check")
        st.rerun()

    st.stop()


# -------------------------
# BUTTONS
# -------------------------
if st.session_state.pending_buttons:
    cols = st.columns(len(st.session_state.pending_buttons))

    for i, label in enumerate(st.session_state.pending_buttons):
        if cols[i].button(label):
            process_user_input(label)
            st.rerun()


# -------------------------
# INPUT
# -------------------------
user_input = st.chat_input("Type here...")

if user_input:
    process_user_input(user_input)
    st.rerun()