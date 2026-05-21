import time
from langchain_core.tools import tool

from app.settings import SCAM_VECTOR_COLLECTION, TECH_VECTOR_COLLECTION
from app.vectorstore import get_vector_store

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


def retrieve_scam_docs(
    user_query: str,
    top_k: int = 5,
    score_threshold: float = 0.4,
):
    """Retrieve scam-safety chunks from the scam_kb PGVector collection."""
    total_start = time.time()
    query_text = (user_query or "").strip()
    if not query_text:
        return [], "", {}

    vector_store = get_vector_store(SCAM_VECTOR_COLLECTION)
    t1 = time.time()
    scored_docs = vector_store.similarity_search_with_score(query_text, k=top_k)
    print(f"[TIMING] SCAM VECTOR SEARCH: {time.time() - t1:.3f}s")

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
    for confidence, doc in ranked_docs[:4]:
        doc.metadata = doc.metadata or {}
        doc.metadata["confidence"] = confidence
        filtered_docs.append(doc)

    context = "\n\n".join([doc.page_content for doc in filtered_docs])
    print(f"[TIMING] SCAM TOTAL RETRIEVAL: {time.time() - total_start:.3f}s")
    return filtered_docs, context, {
        "name": "search_scam_kb",
        "args": {"user_query": user_query},
    }


@tool("search_scam_kb")
def search_scam_kb(user_query: str):
    """Retrieve scam-type modus operandi, red flags, and safety guidance from scam_kb."""
    docs, context, _ = retrieve_scam_docs(user_query=user_query)
    sources = []
    confidences = []
    for doc in docs:
        meta = doc.metadata or {}
        src = meta.get("url") or meta.get("source") or meta.get("title") or ""
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
