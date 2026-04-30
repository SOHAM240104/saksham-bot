import json
import time
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from app.settings import TECH_VECTOR_COLLECTION
from app.vectorstore import get_vector_store

MODEL_NAME = "gpt-4.1-mini"
SUPPORTED_PLATFORMS = {"apple", "samsung", "pixel", "oppo", "xiaomi"}


def _load_system_prompt() -> str:
    system_prompt_path = Path(__file__).resolve().parents[1] / "llm" / "systemprompt.txt"
    if system_prompt_path.exists():
        return system_prompt_path.read_text(encoding="utf-8").strip()
    return "You are a smartphone support assistant. Reply with concise, practical steps."


def _get_llm() -> ChatOpenAI:
    return ChatOpenAI(model=MODEL_NAME, temperature=0)


def build_prompt(query, context, device=None, name="User", os_version=None):
    base_prompt = _load_system_prompt()
    return f"""
{base_prompt}

<runtime_context>
Name: {name}
Device: {(device or "unknown").upper()}
OS Version: {os_version or "unknown"}
</runtime_context>

<knowledge_context>
{context}
</knowledge_context>

<user_query>
{query}
</user_query>
"""


def _chat_reply(user_query: str, state: dict, instruction: str, context: str = "") -> str:
    llm = _get_llm()
    prompt = f"""
{_load_system_prompt()}

<conversation_state>
{json.dumps(state, ensure_ascii=False)}
</conversation_state>

<instruction>
{instruction}
</instruction>

<knowledge_context>
{context}
</knowledge_context>

<user_query>
{user_query}
</user_query>

Respond with plain WhatsApp-ready text.
"""
    response = llm.invoke(prompt)
    return (getattr(response, "content", "") or "").strip()


def retrieve_support_docs(
    user_query,
    device=None,
    os_version=None,
    retrieval_mode="initial",
    top_k=5,
    score_threshold=0.4,
):
    total_start = time.time()
    vector_store = get_vector_store(TECH_VECTOR_COLLECTION)
    filter_dict = {"platform": (device or "").lower()}

    if not filter_dict["platform"]:
        return [], "", {}

    t1 = time.time()
    scored_docs = vector_store.similarity_search_with_score(
        user_query,
        k=top_k,
        filter=filter_dict,
    )
    print(f"[TIMING] VECTOR SEARCH: {time.time() - t1:.3f}s")

    t2 = time.time()
    ranked_docs = sorted(
        [
            (1 - float(score), doc)
            for doc, score in scored_docs
            if (1 - float(score)) >= score_threshold
        ],
        key=lambda item: item[0],
        reverse=True,
    )
    filtered_docs = []
    for confidence, doc in ranked_docs[:3]:
        doc.metadata = doc.metadata or {}
        doc.metadata["confidence"] = confidence
        filtered_docs.append(doc)
    print(f"[TIMING] SCORE + RANK: {time.time() - t2:.3f}s")

    context = "\n\n".join([doc.page_content for doc in filtered_docs])
    print(f"[DEBUG] Context length: {len(context)} chars")
    print(f"[TIMING] TOTAL RETRIEVAL: {time.time() - total_start:.3f}s")

    return filtered_docs, context, {
        "name": "search_support_docs",
        "args": {
            "query": user_query,
            "platform": device,
            "os_version": os_version,
            "retrieval_mode": retrieval_mode,
        },
    }


@tool("search_support_docs")
def search_support_docs(user_query: str, platform: str, os_version: str = "", retrieval_mode: str = "initial"):
    """Retrieve platform-specific troubleshooting documentation."""
    _, context, _ = retrieve_support_docs(
        user_query=user_query,
        device=platform,
        os_version=os_version or None,
        retrieval_mode=retrieval_mode,
    )
    return {"context": context}


@tool("classify_support_mode")
def classify_support_mode(user_query: str):
    """
    Classify whether the user needs technical help or scam help.
    Returns: {"mode": "tech" | "scam" | "unknown"}
    """
    text = (user_query or "").lower()
    scam_keywords = ["scam", "fraud", "otp", "phishing", "fake link", "suspicious"]
    tech_keywords = ["phone", "iphone", "android", "samsung", "ringtone", "wifi", "bluetooth", "settings"]
    if any(k in text for k in scam_keywords):
        return {"mode": "scam"}
    if any(k in text for k in tech_keywords):
        return {"mode": "tech"}
    return {"mode": "unknown"}


@tool("detect_platform")
def detect_platform(user_query: str):
    """
    Detect smartphone platform from user text.
    Returns: {"platform": "apple" | "samsung" | "pixel" | "oppo" | "xiaomi" | "unknown"}
    """
    text = (user_query or "").lower()
    if any(k in text for k in ["iphone", "ipad", "apple", "ios"]):
        return {"platform": "apple"}
    if any(k in text for k in ["samsung", "galaxy"]):
        return {"platform": "samsung"}
    if any(k in text for k in ["pixel", "google"]):
        return {"platform": "pixel"}
    if "oppo" in text:
        return {"platform": "oppo"}
    if any(k in text for k in ["xiaomi", "redmi", "poco", " mi "]):
        return {"platform": "xiaomi"}
    return {"platform": "unknown"}


def call_mode_tool(user_query: str) -> str:
    tool_llm = _get_llm().bind_tools([classify_support_mode], tool_choice="classify_support_mode")
    ai_message = tool_llm.invoke(
        [
            HumanMessage(
                content=f"Classify this user query into tech/scam/unknown: {user_query}",
            )
        ]
    )
    if getattr(ai_message, "tool_calls", None):
        args = ai_message.tool_calls[0].get("args", {})
        mode = str(args.get("mode", "")).lower()
        if mode in {"tech", "scam"}:
            return mode
    return classify_support_mode.invoke({"user_query": user_query}).get("mode", "unknown")


def call_platform_tool(user_query: str) -> str:
    tool_llm = _get_llm().bind_tools([detect_platform], tool_choice="detect_platform")
    ai_message = tool_llm.invoke(
        [
            HumanMessage(
                content=f"Detect platform for this query: {user_query}",
            )
        ]
    )
    if getattr(ai_message, "tool_calls", None):
        args = ai_message.tool_calls[0].get("args", {})
        platform = str(args.get("platform", "")).lower()
        if platform in SUPPORTED_PLATFORMS:
            return platform
    platform = detect_platform.invoke({"user_query": user_query}).get("platform", "unknown")
    return platform if platform in SUPPORTED_PLATFORMS else "unknown"


def run_wati_conversation_step(user_query: str, state: dict) -> tuple[str, dict]:
    state = dict(state or {})
    text = (user_query or "").strip()

    if not text:
        reply = _chat_reply(
            user_query="",
            state=state,
            instruction="Welcome the user and ask whether they want tech help or scam help.",
        )
        return reply, state

    mode = state.get("mode")
    if not mode:
        mode = call_mode_tool(text)
        if mode in {"tech", "scam"}:
            state["mode"] = mode

    if state.get("mode") == "scam":
        reply = _chat_reply(
            user_query=text,
            state=state,
            instruction="Handle this as scam-help conversation and ask the next useful question.",
        )
        return reply, state

    if state.get("mode") != "tech":
        reply = _chat_reply(
            user_query=text,
            state=state,
            instruction="Ask the user to choose between tech help and scam help.",
        )
        return reply, state

    if not state.get("platform"):
        platform = call_platform_tool(text)
        if platform != "unknown":
            state["platform"] = platform
            state["awaiting_issue"] = True
            reply = _chat_reply(
                user_query=text,
                state=state,
                instruction="Platform is known now. Ask user what exact issue they are facing.",
            )
            return reply, state
        reply = _chat_reply(
            user_query=text,
            state=state,
            instruction="Ask user to specify platform: Apple, Samsung, Pixel, Oppo, or Xiaomi.",
        )
        return reply, state

    if state.get("awaiting_issue"):
        state["awaiting_issue"] = False

    docs, context, _ = retrieve_support_docs(
        user_query=text,
        device=state["platform"],
        os_version=None,
        top_k=3,
        score_threshold=0.25,
    )
    _ = docs
    if not context:
        reply = _chat_reply(
            user_query=text,
            state=state,
            instruction="No support docs found. Ask a clarifying question about the phone issue.",
        )
        return reply, state

    response = ""
    for chunk in stream_rag_response(
        user_query=text,
        context=context,
        device=state["platform"],
        name="User",
    ):
        response += chunk
    return (response or _chat_reply(text, state, "Provide concise troubleshooting steps.")), state


def stream_rag_response(user_query, context, device=None, name="User", os_version=None):
    llm = _get_llm()
    prompt = build_prompt(
        query=user_query,
        context=context,
        device=device,
        name=name,
        os_version=os_version,
    )

    start = time.time()
    first_token_time = None
    for chunk in llm.stream(prompt):
        content = getattr(chunk, "content", "")
        if content:
            if first_token_time is None:
                first_token_time = time.time()
                print(f"[TIMING] FIRST TOKEN: {first_token_time - start:.3f}s")
            yield content
    print(f"[TIMING] FULL RESPONSE: {time.time() - start:.3f}s")
