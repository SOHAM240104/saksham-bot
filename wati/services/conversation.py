import logging

from wati.llm.rag_chain import run_wati_conversation_step

logger = logging.getLogger("wati.services.conversation")

_wati_user_state: dict[str, dict] = {}


async def generate_wati_reply(phone: str, message: str) -> str:
    cleaned = (message or "").strip()
    state = _wati_user_state.get(phone, {})
    reply, next_state = run_wati_conversation_step(cleaned, state)
    _wati_user_state[phone] = next_state
    return (reply or "").strip() or "Please share your issue."
