from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pathlib import Path
from app.settings import TECH_VECTOR_COLLECTION
from app.vectorstore import get_vector_store
import time

MODEL_NAME = "gpt-4.1-mini"


# -------------------------
# PROMPT + LLM
# -------------------------
def _load_system_prompt() -> str:
    system_prompt_path = Path(__file__).resolve().parents[1] / "llm" / "systemprompt.txt"
    if system_prompt_path.exists():
        return system_prompt_path.read_text(encoding="utf-8").strip()
    return "System prompt not found. Please ensure 'systemprompt.txt' exists in the 'utils' directory."


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


# -------------------------
# RETRIEVAL 
# -------------------------
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

    # VECTOR SEARCH
    t1 = time.time()

    scored_docs = vector_store.similarity_search_with_score(
        user_query,
        k=top_k,
        filter=filter_dict,
    )

    print(f"[TIMING] VECTOR SEARCH: {time.time() - t1:.3f}s")

    # Convert cosine distance to confidence and keep strong matches only.
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

    # CONTEXT BUILD
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


def call_support_docs_tool(user_query, platform=None, os_version=None, retrieval_mode="initial"):
    """Use LangChain tool calling to resolve retrieval arguments, then retrieve docs."""
    if not platform:
        return [], "", {
            "name": "search_support_docs",
            "args": {
                "query": user_query,
                "platform": platform,
                "os_version": os_version,
                "retrieval_mode": retrieval_mode,
            },
        }

    tool_llm = _get_llm().bind_tools([search_support_docs], tool_choice="search_support_docs")
    tool_message = HumanMessage(
        content=(
            "Call search_support_docs with exact values for this troubleshooting request.\n"
            f"user_query: {user_query}\n"
            f"platform: {platform}\n"
            f"os_version: {os_version or ''}\n"
            f"retrieval_mode: {retrieval_mode}"
        )
    )
    ai_message = tool_llm.invoke([tool_message])

    tool_args = {
        "user_query": user_query,
        "platform": platform,
        "os_version": os_version,
        "retrieval_mode": retrieval_mode,
    }
    if getattr(ai_message, "tool_calls", None):
        called_args = ai_message.tool_calls[0].get("args", {})
        tool_args["user_query"] = called_args.get("user_query", user_query)
        tool_args["platform"] = called_args.get("platform", platform)
        tool_args["os_version"] = called_args.get("os_version", os_version)
        tool_args["retrieval_mode"] = called_args.get("retrieval_mode", retrieval_mode)

    docs, context, _ = retrieve_support_docs(
        user_query=tool_args["user_query"],
        device=tool_args["platform"],
        os_version=tool_args["os_version"] or None,
        retrieval_mode=tool_args["retrieval_mode"],
    )
    return docs, context, {
        "name": "search_support_docs",
        "args": {
            "query": tool_args["user_query"],
            "platform": tool_args["platform"],
            "os_version": tool_args["os_version"],
            "retrieval_mode": tool_args["retrieval_mode"],
        },
    }


# -------------------------
#  STREAM RESPONSE 
# -------------------------
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