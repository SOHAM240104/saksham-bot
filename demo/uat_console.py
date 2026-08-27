"""Saksham UAT Console — Streamlit demo for product/client walkthroughs.

Run:  streamlit run demo/uat_console.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import streamlit as st

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from demo.api_client import SakshamClient, SSEEvent  # noqa: E402

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="Saksham UAT Console", page_icon="💬", layout="wide")

# ── Custom CSS ───────────────────────────────────────────────────────────────

st.markdown(
    """
<style>
    .chat-container { max-width: 700px; margin: 0 auto; }
    .bubble { padding: 10px 16px; border-radius: 16px; margin: 6px 0; display: inline-block; max-width: 85%; line-height: 1.5; }
    .bubble-bot { background: #e8f0fe; color: #1a1a2e; border-bottom-left-radius: 4px; }
    .bubble-user { background: #4a6cf7; color: white; border-bottom-right-radius: 4px; float: right; clear: both; }
    .bubble-agent { background: #fff3cd; color: #664d03; border-bottom-left-radius: 4px; }
    .bubble-wrap { overflow: hidden; }
    .chip-row { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; }
    .handoff-banner { background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px; padding: 12px 16px; margin: 8px 0; text-align: center; }
    .conv-card { background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 8px; padding: 12px 16px; margin: 6px 0; cursor: pointer; }
    .state-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.75rem; font-weight: 600; }
    .state-human_active { background: #ffc107; color: #664d03; }
    .state-bot_active { background: #198754; color: white; }
    .state-resolved { background: #6c757d; color: white; }
    div[data-testid="stHorizontalBlock"] > div { padding: 0 4px; }
</style>
""",
    unsafe_allow_html=True,
)

# ── Session state defaults ───────────────────────────────────────────────────

_DEFAULTS: dict = {
    "api_base": os.getenv("SAKSHAM_API_BASE", "http://127.0.0.1:8000"),
    "senior_jwt": "",
    "agent_jwt": "",
    "chat_history": [],        # list[dict] — {role, text, ui?}
    "pending_chips": None,     # current clickable chip set (or None)
    "handoff_active": False,
    "last_message_id": 0,
    "agent_selected_conv": None,
    "agent_messages": [],
    "pending_send": None,      # {"message": str, "selection_id": str|None} while streaming
}

for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


def _client(jwt: str) -> SakshamClient:
    return SakshamClient(st.session_state.api_base, jwt)


# ── Helpers: JWT minting ─────────────────────────────────────────────────────

def _mint_jwt(role: str, phone_suffix: str) -> str:
    """Call scripts/mint_test_jwt.py and return the raw token string."""
    script = os.path.join(ROOT, "scripts", "mint_test_jwt.py")
    env = {**os.environ}
    env.setdefault("JWT_SECRET_KEY", "local-dev-jwt-secret-saksham-bot")
    try:
        result = subprocess.run(
            [sys.executable, script, "--role", role, "--phone-suffix", phone_suffix],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
            cwd=ROOT,
        )
        if result.returncode != 0:
            return f"ERROR: {result.stderr.strip()}"
        lines = result.stdout.strip().splitlines()
        return lines[-1] if lines else "ERROR: no output"
    except Exception as exc:
        return f"ERROR: {exc}"


# ── Helpers: chat rendering ──────────────────────────────────────────────────

def _render_bubble(role: str, text: str) -> None:
    css_class = {"bot": "bubble-bot", "user": "bubble-user", "techsaathi": "bubble-agent"}.get(role, "bubble-bot")
    align = "right" if role == "user" else "left"
    st.markdown(
        f'<div class="bubble-wrap" style="text-align:{align}"><div class="bubble {css_class}">{text}</div></div>',
        unsafe_allow_html=True,
    )


def _render_handoff_banner() -> None:
    st.markdown(
        '<div class="handoff-banner">⏳ Waiting for a human support agent…</div>',
        unsafe_allow_html=True,
    )


def _apply_sse_event(ev: SSEEvent) -> list[dict]:
    """Apply one SSE event to session state. Returns new history entries for live UI."""
    new_entries: list[dict] = []
    if ev.event == "message":
        role = ev.data.get("role", "bot")
        text = ev.data.get("text", "")
        if text:
            entry = {"role": role, "text": text}
            st.session_state.chat_history.append(entry)
            new_entries.append(entry)
            msg_id = ev.data.get("message_id")
            if msg_id:
                st.session_state.last_message_id = max(
                    st.session_state.last_message_id, msg_id
                )
    elif ev.event == "ui":
        ui_type = ev.data.get("type", "")
        if ui_type == "handoff":
            st.session_state.handoff_active = True
            entry = {"role": "system", "text": "__handoff__"}
            st.session_state.chat_history.append(entry)
            new_entries.append(entry)
            st.session_state.pending_chips = None
        elif ui_type in ("chips", "platform_picker"):
            st.session_state.pending_chips = ev.data
            st.session_state.handoff_active = False
    elif ev.event == "error":
        entry = {
            "role": "bot",
            "text": f"⚠️ {ev.data.get('message', 'Unknown error')}",
        }
        st.session_state.chat_history.append(entry)
        new_entries.append(entry)
    return new_entries


def _render_live_entries(placeholder, entries: list[dict], status_text: str) -> None:
    """Redraw the live turn area (status + bubbles received so far)."""
    with placeholder.container():
        st.caption(status_text)
        for entry in entries:
            if entry["text"] == "__handoff__":
                _render_handoff_banner()
            else:
                _render_bubble(entry["role"], entry["text"])


def _queue_send(message: str, selection_id: str | None = None) -> None:
    """Append the user bubble immediately, then stream the bot reply on the next run."""
    st.session_state.chat_history.append({"role": "user", "text": message})
    st.session_state.pending_chips = None
    st.session_state.pending_send = {
        "message": message,
        "selection_id": selection_id,
    }


def _stream_pending_reply() -> None:
    """If a send is pending, stream SSE with spinner + live bubbles."""
    pending = st.session_state.pending_send
    if not pending:
        return

    st.session_state.pending_send = None
    client = _client(st.session_state.senior_jwt)
    live = st.empty()
    live_entries: list[dict] = []
    _render_live_entries(
        live,
        live_entries,
        "⏳ Bot is thinking… (LLM + RAG may take 10–20s)",
    )

    try:
        with st.spinner("Waiting for Saksham…"):
            for ev in client.stream_message(
                pending["message"],
                pending.get("selection_id"),
            ):
                if ev.event == "status":
                    status_msg = (ev.data.get("message") or "").strip()
                    status_kind = (ev.data.get("status") or "").strip()
                    if status_kind == "thinking" and status_msg:
                        _render_live_entries(live, live_entries, f"⏳ {status_msg}")
                    continue
                added = _apply_sse_event(ev)
                if added:
                    live_entries.extend(added)
                    label = (
                        "✅ Reply received"
                        if ev.event == "message"
                        else "⏳ Still working…"
                    )
                    _render_live_entries(live, live_entries, label)
        live.empty()
    except Exception as exc:
        live.empty()
        st.session_state.chat_history.append(
            {"role": "bot", "text": f"⚠️ API error: {exc}"}
        )


# ── Sidebar: config & auth ───────────────────────────────────────────────────

with st.sidebar:
    st.title("Saksham UAT Console")
    st.caption("Internal demo — not production")
    st.divider()

    st.subheader("API")
    st.session_state.api_base = st.text_input("Base URL", value=st.session_state.api_base)

    st.divider()
    st.subheader("Senior Login")
    sr_phone = st.text_input("Phone suffix", value="8328827545", key="sr_phone")
    if st.button("Mint Senior JWT", key="btn_sr_mint"):
        tok = _mint_jwt("senior", sr_phone)
        if tok.startswith("ERROR"):
            st.error(tok)
        else:
            st.session_state.senior_jwt = tok
            st.success("Senior JWT set")
    if st.session_state.senior_jwt:
        st.code(st.session_state.senior_jwt[:40] + "…", language=None)

    st.divider()
    st.subheader("Agent Login")
    ag_phone = st.text_input("Phone suffix", value="9879878789", key="ag_phone")
    if st.button("Mint Agent JWT", key="btn_ag_mint"):
        tok = _mint_jwt("tech_saathi", ag_phone)
        if tok.startswith("ERROR"):
            st.error(tok)
        else:
            st.session_state.agent_jwt = tok
            st.success("Agent JWT set")
    if st.session_state.agent_jwt:
        st.code(st.session_state.agent_jwt[:40] + "…", language=None)

    st.divider()
    if st.button("🗑 Clear chat", key="btn_clear"):
        st.session_state.chat_history = []
        st.session_state.pending_chips = None
        st.session_state.handoff_active = False
        st.session_state.last_message_id = 0
        st.session_state.pending_send = None
        st.rerun()

# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_chat, tab_agent = st.tabs(["💬 Senior Chat", "🛠 TechSaathi Inbox"])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1: Senior Chat
# ═══════════════════════════════════════════════════════════════════════════════

with tab_chat:
    if not st.session_state.senior_jwt:
        st.info("Mint a Senior JWT in the sidebar to start chatting.")
    else:
        # Render existing conversation (includes user bubble from queued send)
        chat_container = st.container()
        with chat_container:
            for entry in st.session_state.chat_history:
                if entry["text"] == "__handoff__":
                    _render_handoff_banner()
                else:
                    _render_bubble(entry["role"], entry["text"])

        # Stream bot reply under the history while showing a spinner
        if st.session_state.pending_send:
            _stream_pending_reply()
            st.rerun()

        # Handoff polling
        if st.session_state.handoff_active:
            col_poll, _ = st.columns([1, 3])
            with col_poll:
                if st.button("🔄 Check for agent replies", key="btn_poll"):
                    client = _client(st.session_state.senior_jwt)
                    try:
                        msgs = client.get_messages(since_id=st.session_state.last_message_id)
                        new_agent = [
                            m for m in msgs
                            if m.get("tech_saathi_response")
                            and m["id"] > st.session_state.last_message_id
                        ]
                        if new_agent:
                            for m in new_agent:
                                st.session_state.chat_history.append(
                                    {"role": "techsaathi", "text": m["tech_saathi_response"]}
                                )
                                st.session_state.last_message_id = max(
                                    st.session_state.last_message_id, m["id"]
                                )
                            st.rerun()
                        else:
                            st.caption("No new agent replies yet.")
                    except Exception as exc:
                        st.error(f"Poll error: {exc}")

        # Chips / buttons
        if st.session_state.pending_chips:
            chip_data = st.session_state.pending_chips
            options = chip_data.get("options", [])
            cols = st.columns(min(len(options), 5))
            for i, opt in enumerate(options):
                with cols[i % len(cols)]:
                    label = opt["label"]
                    desc = opt.get("description", "")
                    btn_label = f"{label}\n{desc}" if desc else label
                    if st.button(btn_label, key=f"chip_{opt['id']}"):
                        _queue_send(label, selection_id=opt["id"])
                        st.rerun()

        # Free-text input
        with st.form("chat_form", clear_on_submit=True):
            user_input = st.text_input(
                "Type a message…",
                key="chat_input",
                label_visibility="collapsed",
                placeholder="Type a message…",
            )
            submitted = st.form_submit_button("Send")
            if submitted and user_input.strip():
                _queue_send(user_input.strip())
                st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: TechSaathi Agent Inbox
# ═══════════════════════════════════════════════════════════════════════════════

with tab_agent:
    if not st.session_state.agent_jwt:
        st.info("Mint an Agent JWT in the sidebar to view the inbox.")
    else:
        client = _client(st.session_state.agent_jwt)

        col_list, col_detail = st.columns([1, 2])

        # ── Left: conversation list ──────────────────────────────────────

        with col_list:
            st.subheader("Conversations")
            if st.button("🔄 Refresh", key="btn_refresh_inbox"):
                st.session_state.agent_selected_conv = None
                st.session_state.agent_messages = []

            try:
                conversations = client.list_conversations()
            except Exception as exc:
                st.error(f"Failed to load inbox: {exc}")
                conversations = []

            if not conversations:
                st.caption("No conversations in queue.")

            for conv in conversations:
                state = conv.get("state", "")
                name = conv.get("senior_name") or f"Senior #{conv.get('senior_id', '?')}"
                preview = (conv.get("preview") or "")[:80]
                badge_cls = f"state-{state}"
                cid = conv["conversation_id"]

                st.markdown(
                    f"""<div class="conv-card">
                        <strong>{name}</strong>
                        <span class="state-badge {badge_cls}">{state.replace('_', ' ')}</span><br>
                        <small>{preview}</small>
                    </div>""",
                    unsafe_allow_html=True,
                )
                if st.button(f"Open #{cid}", key=f"open_conv_{cid}_{conv.get('thread_id', '')}"):
                    st.session_state.agent_selected_conv = cid
                    try:
                        st.session_state.agent_messages = client.get_conversation_messages(cid)
                    except Exception as exc:
                        st.error(f"Failed: {exc}")
                        st.session_state.agent_messages = []
                    st.rerun()

        # ── Right: conversation detail ───────────────────────────────────

        with col_detail:
            sel = st.session_state.agent_selected_conv
            if sel is None:
                st.caption("Select a conversation from the list.")
            else:
                st.subheader(f"Conversation #{sel}")

                # Reload messages
                col_r, col_res = st.columns(2)
                with col_r:
                    if st.button("🔄 Reload messages", key="btn_reload_msgs"):
                        try:
                            st.session_state.agent_messages = client.get_conversation_messages(sel)
                        except Exception as exc:
                            st.error(str(exc))
                        st.rerun()
                with col_res:
                    if st.button("✅ Resolve", key="btn_resolve"):
                        try:
                            result = client.resolve_conversation(sel)
                            st.success(
                                f"Resolved thread {result.get('resolved_thread_id')} "
                                f"→ new chatbot thread {result.get('new_chatbot_thread_id')}"
                            )
                            st.session_state.agent_selected_conv = None
                            st.session_state.agent_messages = []
                            time.sleep(0.5)
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Resolve failed: {exc}")

                # Message history
                msgs = st.session_state.agent_messages
                if not msgs:
                    st.caption("No messages.")
                for m in msgs:
                    uq = (m.get("user_question") or "").strip()
                    ar = (m.get("ai_response") or "").strip()
                    ts = (m.get("tech_saathi_response") or "").strip()
                    src = (m.get("message_source") or "").strip()

                    if uq:
                        _render_bubble("user", uq)
                    if ar:
                        _render_bubble("bot", ar)
                    if ts:
                        _render_bubble("techsaathi", ts)

                # Reply form
                with st.form("agent_reply_form", clear_on_submit=True):
                    agent_text = st.text_input(
                        "Reply as agent…",
                        key="agent_reply_input",
                        label_visibility="collapsed",
                        placeholder="Type a reply…",
                    )
                    if st.form_submit_button("Send Reply"):
                        if agent_text.strip():
                            try:
                                client.send_agent_reply(sel, agent_text.strip())
                                st.session_state.agent_messages = client.get_conversation_messages(sel)
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Reply failed: {exc}")
