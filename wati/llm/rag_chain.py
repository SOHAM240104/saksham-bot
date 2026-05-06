import time
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from app.settings import TECH_VECTOR_COLLECTION
from app.vectorstore import get_vector_store

MODEL_NAME = "gpt-4.1-mini"


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
    query_text = (user_query or "").strip()
    if os_version:
        query_text = f"{query_text}\nos_version: {os_version}"

    if not filter_dict["platform"]:
        return [], "", {}

    t1 = time.time()
    scored_docs = vector_store.similarity_search_with_score(
        query_text,
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
    docs, context, _ = retrieve_support_docs(
        user_query=user_query,
        device=platform,
        os_version=os_version or None,
        retrieval_mode=retrieval_mode,
    )
    sources = []
    confidences = []
    for doc in docs:
        meta = doc.metadata or {}
        src = (
            meta.get("source")
            or meta.get("title")
            or meta.get("doc_id")
            or meta.get("id")
            or ""
        )
        if src:
            sources.append(str(src))
        confidence = meta.get("confidence")
        if isinstance(confidence, (int, float)):
            confidences.append(float(confidence))

    avg_confidence = (sum(confidences) / len(confidences)) if confidences else 0.0
    return {
        "context": context,
        "sources": sources,
        "confidence_score": avg_confidence,
    }


def stream_rag_response(user_query, context, device=None, name="User", os_version=None):
    llm = ChatOpenAI(model=MODEL_NAME, temperature=0)
    prompt = f"""
You are a smartphone support assistant.
Provide concise, practical troubleshooting guidance based on the given documentation context.

<runtime_context>
Name: {name}
Device: {(device or "unknown").upper()}
OS Version: {os_version or "unknown"}
</runtime_context>

<knowledge_context>
{context}
</knowledge_context>

<user_query>
{user_query}
</user_query>
"""

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
